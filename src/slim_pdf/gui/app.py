"""Main window: open a PDF, pick a field or click to place, sign, repeat."""

from __future__ import annotations

import html
import platform
import sys
from importlib import metadata
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QLocale, Qt, QTranslator, QUrl, qVersion
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QInputDialog,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import certinfo, forms, i18n, insert, pages, secretstore, systemstore, validation
from ..config import Config, load_config
from ..i18n import N_, _, ngettext
from ..signing import (
    PasswordError,
    SigningBlocked,
    SigningError,
    _atomic_write,
    check_target,
    inspect_pdf,
    open_signer,
    printable,
    sign_pdf,
    stamp_size,
)
from . import background, ribbon, state, watchdog
from .certdialog import details_dialog
from .screen import fit_to_screen, scrollable
from .settings import SettingsDialog
from .dialogs import ask_secret
from .formpanel import FormPanel
from .insertdialog import TextDialog
from .document import PdfDocument, render_stamp
from .pageview import PageView
from .pagetools import PagesPanel

HOME_TAB, EDIT_TAB = 0, 1  # ribbon tabs with their own side panel
STATUS_COLORS = {validation.VALID: "#2e7d32", validation.UNTRUSTED: "#ef8c00",
                 validation.INVALID: "#c62828", validation.UNSUPPORTED: "#757575"}


def _dot(color: str) -> QIcon:
    pm = QPixmap(14, 14)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(1, 1, 12, 12)
    p.end()
    return QIcon(pm)


HINT = N_("Wheel: zoom, drag: move the page. Click an empty field to sign it, or use "
          "Place signature…; a placed signature is moved by dragging and resized at its "
          "corner or with Ctrl+wheel.")
PLACE_HINT = N_("Click where the signature should go, or on an empty (dashed) field. "
                "Esc cancels.")


def _when(dt) -> str:
    return f"{dt:%Y-%m-%d %H:%M:%S %Z}".strip() if dt else ""


def _status_text(status: str) -> str:
    """The word shown for a validation status code."""
    return {validation.VALID: _("valid"), validation.UNTRUSTED: _("untrusted"),
            validation.INVALID: _("invalid"),
            validation.UNSUPPORTED: _("not checked")}.get(status, status)


def _status_count(status: str, n: int) -> str:
    """'2 valid' etc. for the status bar summary."""
    if status == validation.VALID:
        return ngettext("{n} valid", "{n} valid", n).format(n=n)
    if status == validation.UNTRUSTED:
        return ngettext("{n} untrusted", "{n} untrusted", n).format(n=n)
    if status == validation.UNSUPPORTED:
        return ngettext("{n} not checked", "{n} not checked", n).format(n=n)
    return ngettext("{n} invalid", "{n} invalid", n).format(n=n)


class MainWindow(QMainWindow):
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.doc: PdfDocument | None = None
        self.path: Path | None = None
        self.pdf_password: str | None = None
        self.info = None
        self.form_fields: list[forms.FormField] = []
        # what Text…/Image… adds once a box is drawn: ("text", text, size,
        # colour) or ("image", path, aspect ratio)
        self._insert: tuple | None = None
        # The document is edited in memory (page tools); self.path is where it
        # came from or was last saved to.
        self.data: bytes | None = None
        self.dirty = False
        self.undo_stack: list[bytes] = []

        self.setWindowTitle(_("Slim PDF"))
        fit_to_screen(self, 1100, 900)
        self.view = PageView()
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(
            lambda pos: self._show_page_menu(self.view.mapToGlobal(pos)))
        self.view.targetChanged.connect(self._update_target)
        self.view.pageChanged.connect(self._sync_page_spin)
        self.view.cropSelected.connect(self._crop_selected)
        self.view.insertSelected.connect(self._insert_selected)
        self.view.placeModeChanged.connect(self._place_mode_changed)
        self.view.fieldActivated.connect(self._field_activated)
        self.view.formFieldActivated.connect(self.show_form_field)
        self.setCentralWidget(self.view)

        self._build_actions()
        self._build_sign_panel()
        self._build_pages_panel()
        self._build_ribbon()
        self._update_page_actions()
        self.statusBar().showMessage(_(HINT))
        self._reload_certs()
        self._update_target()

    # ribbon -------------------------------------------------------------------

    def _build_actions(self) -> None:
        """Every command the ribbon offers, as actions of the window, so their
        shortcuts work wherever the focus is."""
        def act(text, slot, shortcut=None, icon="", tip="", page=False):
            a = QAction(text, self)
            if slot is not None:
                a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if icon:
                a.setIcon(ribbon.icon(icon))
            a.setToolTip(tip or text)
            self.addAction(a)
            if page:  # needs a document
                self.page_actions.append(a)
            return a

        self.page_actions: list[QAction] = []
        self.open_act = act(_("Open…"), self.open_dialog, QKeySequence.Open, "folder-open")
        self.save_act = act(_("Save"), self.save, None, "device-floppy",
                            _("Save the edited document"), True)
        self.save_as_act = act(_("Save as…"), self.save_as, "Ctrl+Shift+S", "file-export",
                               page=True)
        self.undo_act = act(_("Undo"), self.undo, QKeySequence.Undo, "arrow-back-up",
                            page=True)
        # Esc cancels whatever is going on (placing, cropping, adding); not
        # in the ribbon, which has the specific "remove" below
        self.escape_act = act(_("Cancel"), self._escape, Qt.Key_Escape)
        self.clear_act = act(_("Remove signature box"), self.clear_target, None, "eraser",
                             _("Remove the signature box placed on the page, or deselect "
                               "the chosen field (Esc)"))
        self.settings_act = act(_("Settings…"), self.settings_dialog, QKeySequence.Preferences,
                                "settings")
        # what the stamp shows; the certificate is picked in the ribbon's combo box
        self.cert_combo = QComboBox()
        self.cert_combo.setToolTip(_("The certificate to sign with"))
        self.cert_combo.setMinimumContentsLength(14)
        self.cert_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cert_combo.currentIndexChanged.connect(self._cert_changed)
        self.image_check = act(_("Image"), None, None, "photo",
                               _("The stamp shows the certificate's signature image"))
        self.text_check = act(_("Text"), None, None, "typography",
                              _("The stamp shows the signer's name and the date"))
        for a in (self.image_check, self.text_check):
            a.setCheckable(True)
            a.setChecked(True)
            a.toggled.connect(self._cert_changed)

        self.prev_act = act(_("Previous"), lambda: self.view.go_to(self.view.page - 1),
                            QKeySequence.MoveToPreviousPage, "chevron-left")
        self.next_act = act(_("Next"), lambda: self.view.go_to(self.view.page + 1),
                            QKeySequence.MoveToNextPage, "chevron-right")
        self.zoom_out_act = act(_("Zoom out"), lambda: self.view.set_zoom(self.view.zoom / 1.2),
                                QKeySequence.ZoomOut, "zoom-out")
        self.fit_act = act(_("Fit"), lambda: self.view.set_zoom(None), None, "viewport-wide",
                           _("Fit the page into the window"))
        self.zoom_in_act = act(_("Zoom in"), lambda: self.view.set_zoom(self.view.zoom * 1.2),
                               QKeySequence.ZoomIn, "zoom-in")
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.valueChanged.connect(lambda v: self.view.go_to(v - 1))
        self.page_count = QLabel(" / 0 ")

        # Page edits are also in the pages' context menu (right-click a
        # thumbnail or the page).
        self.rotate_left_act = act(_("Rotate left"), lambda: self.rotate_pages(-90), "Ctrl+L",
                                   "rotate-2", page=True)
        self.rotate_right_act = act(_("Rotate right"), lambda: self.rotate_pages(90), "Ctrl+R",
                                    "rotate-clockwise-2", page=True)
        self.crop_act = act(_("Crop…"), self._toggle_crop, None, "crop", page=True)
        self.crop_act.setCheckable(True)
        self.uncrop_act = act(_("Uncrop"), self.reset_crop, None, "arrows-maximize", page=True)
        self.delete_act = act(_("Delete"), self.delete_pages, None, "trash", page=True)
        # the menu names the pages ("Delete page 3"); the ribbon can't know
        # which ones until clicked
        self.delete_pages_act = act(_("Delete pages"), self.delete_pages, None, "trash",
                                    _("Delete the pages selected in the Pages panel, or the "
                                      "current one"), True)


        self.insert_text_act = act(_("Text…"), self.insert_text, None, "text-plus",
                                   _("Add text to the page"), True)
        self.insert_image_act = act(_("Image…"), self.insert_image, None, "photo-plus",
                                    _("Add an image (PNG, JPEG or PDF) to the page"), True)

        self.about_act = act(_("About Slim PDF"), self.about, None, "info-circle")
        self.about_act.setMenuRole(QAction.AboutRole)
        self.docs_act = act(_("Documentation"), self.open_docs, QKeySequence.HelpContents,
                            "book")

    def _build_ribbon(self) -> None:
        rb = ribbon.Ribbon()
        self.ribbon = rb

        self.sign_act.setIcon(ribbon.icon("signature"))
        home = rb.tab(_("Home"))
        g = home.group(_("File"))
        g.add(self.open_act)
        g.add(self.save_act)
        g.add(self.save_as_act, large=False)
        g.add(self.undo_act, large=False)
        g = home.group(_("Sign"))
        g.add(self.sign_act)
        g.add(self.clear_act, large=False)
        # the signature's settings as a little form, so each says what it is
        g = home.group(_("Signature"))
        form = QWidget()
        fl = QFormLayout(form)
        fl.setContentsMargins(4, 0, 4, 0)
        fl.setVerticalSpacing(2)
        fl.addRow(_("Sign with:"), self.cert_combo)
        stamp = QWidget()
        row = QHBoxLayout(stamp)
        row.setContentsMargins(0, 0, 0, 0)
        self.image_box = self._check_box(self.image_check)
        self.text_box = self._check_box(self.text_check)
        row.addWidget(self.image_box)
        row.addWidget(self.text_box)
        row.addStretch()
        fl.addRow(_("Stamp shows:"), stamp)
        g.add_widget(form)
        g = home.group(_("Options"))
        g.add(self.settings_act)

        edit = rb.tab(_("Edit"))
        g = edit.group(_("Insert"))
        g.add(self.insert_text_act)
        g.add(self.insert_image_act)

        pages_tab = rb.tab(_("Pages"))
        g = pages_tab.group(_("Rotate"))
        g.add(self.rotate_left_act)
        g.add(self.rotate_right_act)
        g = pages_tab.group(_("Crop"))
        g.add(self.crop_act)
        g.add(self.uncrop_act)
        g = pages_tab.group(_("Delete"))
        g.add(self.delete_pages_act)

        view = rb.tab(_("View"))
        g = view.group(_("Navigate"))
        g.add(self.prev_act)
        spin = QWidget()
        row = QHBoxLayout(spin)
        row.setContentsMargins(4, 0, 4, 0)
        row.addWidget(self.page_spin)
        row.addWidget(self.page_count)
        g.add_widget(spin)
        g.add(self.next_act)
        g = view.group(_("Zoom"))
        g.add(self.zoom_out_act)
        g.add(self.fit_act)
        g.add(self.zoom_in_act)
        g = view.group(_("Panels"))
        panel = self.pages_dock.toggleViewAction()
        panel.setIcon(ribbon.icon("layout-sidebar"))
        g.add(panel)

        help_tab = rb.tab(_("Help"))
        g = help_tab.group(_("Help"))
        g.add(self.docs_act)
        g.add(self.about_act)

        rb.refit()
        rb.currentChanged.connect(self._ribbon_tab_changed)
        self.setMenuWidget(rb)  # where the menu bar would be

    @staticmethod
    def _check_box(action: QAction) -> QCheckBox:
        """A check box showing (and setting) a checkable action."""
        box = QCheckBox(action.text())
        box.setToolTip(action.toolTip())
        box.setChecked(action.isChecked())
        box.toggled.connect(action.setChecked)
        action.toggled.connect(box.setChecked)
        return box

    def page_menu(self) -> QMenu:
        """The pages' context menu, for the selected pages (or the current one)."""
        target = self.target_pages()
        self.delete_act.setText(
            _("Delete page {page}").format(page=target[0] + 1) if len(target) == 1
            else ngettext("Delete {n} page", "Delete {n} pages", len(target)).format(
                n=len(target)))
        menu = QMenu(self)
        menu.addActions([self.rotate_left_act, self.rotate_right_act])
        menu.addSeparator()
        menu.addActions([self.crop_act, self.uncrop_act])
        menu.addSeparator()
        menu.addAction(self.delete_act)
        return menu

    def _show_page_menu(self, global_pos) -> None:
        if self.data is not None:
            self._exec_menu(self.page_menu(), global_pos)

    def _exec_menu(self, menu: QMenu, global_pos) -> None:
        menu.exec(global_pos)

    def _build_pages_panel(self) -> None:
        self.thumbs = PagesPanel()
        self.thumbs.pageActivated.connect(self.view.go_to)
        self.thumbs.menuRequested.connect(self._show_page_menu)
        self.thumbs.deleteRequested.connect(self.delete_pages)  # Delete key on thumbnails
        self.thumbs.moveRequested.connect(self.move_pages)  # drag and drop, Alt+Up/Down
        self.view.pageChanged.connect(self.thumbs.show_page)
        dock = QDockWidget(_("Pages"))
        dock.setWidget(self.thumbs)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self.resizeDocks([dock], [self.thumbs.minimumWidth() + 20], Qt.Horizontal)
        self.pages_dock = dock

    def _escape(self) -> None:
        if self.view.insert_mode:
            self.view.set_insert_mode(False)
            self._insert = None
            self.statusBar().showMessage(_(HINT))
        elif self.view.place_mode:
            self.view.set_place_mode(False)
        elif self.view.crop_mode:
            self.crop_act.setChecked(False)
            self.view.set_crop_mode(False)
            self.statusBar().showMessage(_(HINT))
        else:
            self.view.clear_target()

    def clear_target(self) -> None:
        """No signature box, no chosen field, not placing one."""
        self.view.set_place_mode(False)
        self.view.clear_target()

    def _build_sign_panel(self) -> None:
        panel = QWidget()
        lay = QVBoxLayout(panel)

        lay.addWidget(QLabel("<b>{}</b>".format(_("Target"))))
        self.target_label = QLabel()
        self.target_label.setWordWrap(True)
        lay.addWidget(self.target_label)

        self.sign_act = QAction(_("Sign…"), self, shortcut=QKeySequence("Ctrl+S"),
                                triggered=self.sign)
        self.addAction(self.sign_act)
        self.sign_btn = QPushButton(_("Sign…"))
        self.sign_btn.setDefault(True)
        self.sign_btn.setMinimumHeight(34)
        self.sign_btn.clicked.connect(self.sign_act.trigger)
        self.sign_act.changed.connect(
            lambda: self.sign_btn.setEnabled(self.sign_act.isEnabled()))
        lay.addWidget(self.sign_btn)

        lay.addSpacing(8)
        lay.addWidget(QLabel("<b>{}</b>".format(_("Signature fields"))))
        self.fields_list = QListWidget()
        self.fields_list.itemClicked.connect(self._field_clicked)
        lay.addWidget(self.fields_list, 1)

        lay.addSpacing(8)
        lay.addWidget(QLabel("<b>{}</b>".format(_("Signatures"))))
        self.sigs_list = QListWidget()
        self.sigs_list.setToolTip(_("Double-click for the full validation report"))
        self.sigs_list.itemClicked.connect(self._signature_clicked)
        self.sigs_list.itemDoubleClicked.connect(self.show_signature_details)
        lay.addWidget(self.sigs_list, 1)
        self.reports: list[validation.SignatureReport] = []

        # The right-hand panel shows the tools of the ribbon tab in use:
        # signing on Home, the form on Edit, nothing (more room) elsewhere.
        self.sign_panel = scrollable(panel)  # scrolls on small screens, below the ribbon
        self.form_panel = FormPanel()
        self.form_panel.applyRequested.connect(self.fill_form)
        self.side_stack = QStackedWidget()
        self.side_stack.addWidget(self.sign_panel)
        self.side_stack.addWidget(self.form_panel)
        dock = QDockWidget(_("Sign"))
        dock.setWidget(self.side_stack)
        dock.setFeatures(QDockWidget.DockWidgetMovable)
        dock.setMinimumWidth(280)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        self.side_dock = dock

    SIDE_PANELS = {HOME_TAB: N_("Sign"), EDIT_TAB: N_("Form")}  # ribbon tab -> panel title

    def _ribbon_tab_changed(self, index: int) -> None:
        title = self.SIDE_PANELS.get(index)
        self.side_dock.setVisible(title is not None)
        if title is not None:
            self.side_stack.setCurrentIndex(index)
            self.side_dock.setWindowTitle(_(title))

    def show_tab(self, index: int) -> None:
        self.ribbon.setCurrentIndex(index)
        self._ribbon_tab_changed(index)  # also when it was the current one already

    # certificates -------------------------------------------------------------

    def _reload_certs(self, select: str | None = None) -> None:
        select = select or self.cert_combo.currentData() or self.config.default
        self.cert_combo.blockSignals(True)
        self.cert_combo.clear()
        for name in self.config.certs:
            self.cert_combo.addItem(name, name)
        idx = self.cert_combo.findData(select)
        self.cert_combo.setCurrentIndex(max(idx, 0))
        self.cert_combo.blockSignals(False)
        self._cert_changed()

    def current_profile(self):
        name = self.cert_combo.currentData()
        return self.config.certs.get(name) if name else None

    def _cert_changed(self, *_args) -> None:
        p = self.current_profile()
        pixmap, aspect = None, 3.0
        if p is not None:  # the stamp as signing will draw it, with the stored certificate
            with_image, with_text = self.image_check.isChecked(), self.text_check.isChecked()
            params = certinfo.text_params(certinfo.load(p.certificate), p)
            try:
                w, h = stamp_size(p, params, with_image=with_image, with_text=with_text)
            except (OSError, ValueError, SigningError):  # e.g. a missing image
                w, h = 3.0, 1.0
            pm = render_stamp(p, params, with_image=with_image, with_text=with_text,
                              height_px=STAMP_PREVIEW_PX)
            pixmap, aspect = (None if pm.isNull() else pm), w / h
        self.view.set_signature_appearance(pixmap, aspect)
        self._update_target()

    def about(self) -> None:
        QMessageBox.about(self, _("About Slim PDF"), about_text())

    def open_docs(self) -> None:
        QDesktopServices.openUrl(QUrl(DOCS_URL))

    def settings_dialog(self) -> None:
        """Settings…, also for the certificates (their own tab)."""
        dlg = SettingsDialog(self.config, self)
        accepted = dlg.exec()
        if not (accepted or dlg.certs.changed):
            return
        # after Cancel, dlg.config is the old settings with the saved profiles
        trust_changed = dlg.config.trust != self.config.trust
        self.config = dlg.config
        if dlg.certs.changed:
            self._reload_certs()
        if trust_changed and self.data is not None:
            self.statusBar().showMessage(self._verify() or _(HINT))
        if dlg.language_changed:
            QMessageBox.information(self, _("Settings"),
                                    _("The new language is used after restarting Slim PDF."))

    # documents ----------------------------------------------------------------

    def open_dialog(self) -> None:
        if not self._maybe_save():
            return
        # where the last file came from, also in an earlier session
        start = state.last_folder() or (str(self.path.parent) if self.path else "")
        path, _filter = QFileDialog.getOpenFileName(self, _("Open PDF"), start,
                                                    _("PDF") + " (*.pdf)")
        if path:
            self.open(Path(path))

    def open(self, path: Path, password: str | None = None, *, page: int = 0) -> bool:
        try:
            data = Path(path).read_bytes()
            info = inspect_pdf(data, password)
        except PasswordError:  # required, or the one given was wrong
            pw = ask_secret(self, _("Password for {name}").format(name=path.name))
            return pw is not None and self.open(path, pw, page=page)
        except SigningError as e:
            QMessageBox.critical(self, _("Open"), str(e))
            return False
        except Exception as e:  # malformed PDFs raise all sorts of things
            QMessageBox.critical(self, _("Open"),
                                 _("Cannot open {path}:\n{error}").format(path=path, error=e))
            return False

        self.path, self.pdf_password = Path(path), password
        state.remember_folder(self.path.parent)
        self.undo_stack = []
        self._show(data, info, page, dirty=False)
        return True

    def _show(self, data: bytes, info, page: int, *, dirty: bool) -> None:
        """Display ``data`` (already inspected as ``info``)."""
        if self.doc is not None:
            self.doc.close()
        self.data, self.info, self.dirty = data, info, dirty
        self.doc = PdfDocument(data, self.pdf_password)
        try:
            self.form_fields = forms.form_fields(data, self.pdf_password)
        except Exception:  # noqa: BLE001 - a broken form never blocks viewing
            self.form_fields = []
        page = min(page, len(self.doc) - 1)
        self.view.set_document(self.doc, info.fields, page, self.form_fields)
        self.form_panel.set_fields(self.form_fields)
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(len(self.doc))
        self.page_spin.setValue(page + 1)
        self.page_spin.blockSignals(False)
        self.page_count.setText(_(" / {count} ").format(count=len(self.doc)))
        self._fill_fields()
        self.thumbs.set_document(self.doc, page)
        self._update_title()
        message = self._verify() or _(HINT)
        if info.certification is not None:
            message = _("Document is certified ({name}). {message}").format(
                name=info.certification.name, message=message)
        self.statusBar().showMessage(message)
        self._update_page_actions()

    def _update_title(self) -> None:
        name = self.path.name if self.path else _("Slim PDF")
        self.setWindowTitle(_("{modified}{name} – Slim PDF").format(
            modified="*" if self.dirty else "", name=name))

    def _update_page_actions(self) -> None:
        has_doc = self.data is not None
        for a in self.page_actions:
            a.setEnabled(has_doc)
        self.save_act.setEnabled(has_doc and self.dirty)
        self.undo_act.setEnabled(bool(self.undo_stack))

    # page tools -----------------------------------------------------------------

    def target_pages(self) -> list[int]:
        """Pages selected in the thumbnail panel, else the current page."""
        selected = self.thumbs.selected_pages()
        return selected if len(selected) > 1 else [self.view.page]

    def _confirm_breaking(self, message: str) -> bool:
        """``message`` has a ``{names}`` placeholder for the signatures."""
        names = pages.signature_names(self.data, self.pdf_password)
        if not names:
            return True
        return QMessageBox.warning(
            self, _("Existing signatures"), message.format(names=", ".join(names)),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def apply_edit(self, new_data: bytes, message: str, page: int | None = None) -> None:
        self.undo_stack.append(self.data)
        self._show(new_data, inspect_pdf(new_data, self.pdf_password),
                   self.view.page if page is None else page, dirty=True)
        self.statusBar().showMessage(
            _("{message} (not saved yet)").format(message=message), 10000)

    def undo(self) -> None:
        if not self.undo_stack:
            return
        data = self.undo_stack.pop()
        self._show(data, inspect_pdf(data, self.pdf_password), self.view.page,
                   dirty=bool(self.undo_stack))
        self.statusBar().showMessage(_("Undone"), 5000)

    def rotate_pages(self, angle: int) -> None:
        if self.data is None:
            return
        target = self.target_pages()
        if not self._confirm_breaking(_(
                "Rotating pages will invalidate the signature(s) {names}.\n\n"
                "Edit pages before signing. Continue anyway?")):
            return
        if len(target) == 1:
            message = _("Rotated page {page}").format(page=target[0] + 1)
        else:
            message = ngettext("Rotated {n} page", "Rotated {n} pages",
                               len(target)).format(n=len(target))
        self.apply_edit(pages.rotate(self.data, target, angle, self.pdf_password), message)

    def _toggle_crop(self) -> None:
        on = self.crop_act.isChecked() and self.data is not None
        self.view.set_crop_mode(on)
        if on:
            self.statusBar().showMessage(
                _("Drag a rectangle around the area to keep. Esc cancels."))

    def _crop_selected(self, page: int, rect) -> None:
        self.crop_act.setChecked(False)
        self.view.set_crop_mode(False)
        box = self.doc.rect_to_pdf(page, rect.x(), rect.y(), rect.width(), rect.height())
        selected = self.thumbs.selected_pages()
        options = [([page], _("This page ({page})").format(page=page + 1)),
                   (list(range(len(self.doc))), _("All pages"))]
        if len(selected) > 1:
            options.insert(1, (selected, _("Selected pages ({count})").format(
                count=len(selected))))
        choices = [label for _pages, label in options]
        choice, ok = QInputDialog.getItem(self, _("Crop"), _("Apply to:"), choices, 0, False)
        if not ok:
            return
        target = options[choices.index(choice)][0]
        if not self._confirm_breaking(_(
                "Cropping pages will invalidate the signature(s) {names}.\n\n"
                "Edit pages before signing. Continue anyway?")):
            return
        try:
            new = pages.crop(self.data, target, box=box, password=self.pdf_password)
        except (pages.PageError, SigningError) as e:
            QMessageBox.critical(self, _("Crop"), str(e))
            return
        if len(target) == 1:
            message = _("Cropped page {page}").format(page=target[0] + 1)
        else:
            message = ngettext("Cropped {n} page", "Cropped {n} pages",
                               len(target)).format(n=len(target))
        self.apply_edit(new, message)

    def reset_crop(self) -> None:
        if self.data is None:
            return
        target = self.target_pages()
        if not self._confirm_breaking(_(
                "Uncropping pages will invalidate the signature(s) {names}.\n\n"
                "Edit pages before signing. Continue anyway?")):
            return
        if len(target) == 1:
            message = _("Uncropped page {page}").format(page=target[0] + 1)
        else:
            message = ngettext("Uncropped {n} page", "Uncropped {n} pages",
                               len(target)).format(n=len(target))
        self.apply_edit(pages.crop(self.data, target, reset=True, password=self.pdf_password),
                        message)


    def delete_pages(self) -> None:
        if self.data is None:
            return
        target = self.target_pages()
        if len(target) == len(self.doc):
            QMessageBox.information(self, _("Delete pages"),
                                    _("A document can't lose all its pages."))
            return
        if not self._confirm_breaking(_(
                "Deleting pages will invalidate the signature(s) {names}.\n\n"
                "Edit pages before signing. Continue anyway?")):
            return
        try:
            new = pages.delete(self.data, target, self.pdf_password)
        except (pages.PageError, SigningError) as e:
            QMessageBox.critical(self, _("Delete pages"), str(e))
            return
        if len(target) == 1:
            message = _("Deleted page {page}").format(page=target[0] + 1)
        else:
            message = ngettext("Deleted {n} page", "Deleted {n} pages",
                               len(target)).format(n=len(target))
        self.apply_edit(new, message)
    def move_pages(self, target: list[int], to: int) -> None:
        """Move ``target`` pages before page ``to`` (``len(doc)``: to the end)."""
        if self.data is None:
            return
        moved = sorted(set(target))
        order = ([i for i in range(to) if i not in moved] + moved
                 + [i for i in range(to, len(self.doc)) if i not in moved])
        if order == list(range(len(self.doc))):
            return  # dropped where they were
        if not self._confirm_breaking(_(
                "Moving pages will invalidate the signature(s) {names}.\n\n"
                "Edit pages before signing. Continue anyway?")):
            return
        try:
            new = pages.move(self.data, moved, to, self.pdf_password)
        except (pages.PageError, SigningError) as e:
            QMessageBox.critical(self, _("Move pages"), str(e))
            return
        first = order.index(moved[0])  # where the moved pages are now
        if len(moved) == 1:
            message = _("Moved page {page} to position {pos}").format(
                page=moved[0] + 1, pos=first + 1)
        else:
            message = ngettext("Moved {n} page", "Moved {n} pages", len(moved)).format(
                n=len(moved))
        self.apply_edit(new, message, page=first)
        self.thumbs.clearSelection()
        for row in range(first, first + len(moved)):  # still selected, for further moves
            self.thumbs.item(row).setSelected(True)

    # forms ----------------------------------------------------------------------

    def show_form_field(self, name: str) -> None:
        """A form field clicked on the page: the form panel, cursor in it."""
        self.show_tab(EDIT_TAB)
        self.form_panel.focus(name)

    def fill_form(self, values: dict) -> None:
        """Fill in ``values`` (from the form panel's Apply), as an edit."""
        if self.data is None or not values:
            return
        try:
            new = forms.fill(self.data, values, self.pdf_password)
        except SigningError as e:  # forms.FormError among them
            QMessageBox.critical(self, _("Fill in form"), str(e))
            return
        if not self._confirm_signatures_survive(new, _(
                "Filling in these fields will invalidate the signature(s) {names}.\n\n"
                "Fill in forms before signing. Continue anyway?")):
            return
        n = len(values)
        self.apply_edit(new, ngettext("Filled in {n} field", "Filled in {n} fields", n).format(
            n=n))

    def _confirm_signatures_survive(self, new: bytes, message: str) -> bool:
        """For edits that signatures may allow (filling in forms): check the
        result, and ask if a signature valid now would become invalid."""
        before = {r.field: r.status for r in self.reports}
        if not before:
            return True
        trust, password = self.config.trust, self.pdf_password
        try:
            after = background.run(self, lambda ask: validation.verify_pdf(
                new, trust=trust, password=password))
        except Exception:  # noqa: BLE001 - can't tell: let the user decide
            after = [validation.SignatureReport(field=f, status=validation.INVALID)
                     for f in before]
        broken = [printable(r.field) for r in after
                  if r.status == validation.INVALID
                  and before.get(r.field, validation.INVALID) != validation.INVALID]
        if not broken:
            return True
        return QMessageBox.warning(
            self, _("Existing signatures"), message.format(names=", ".join(broken)),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    # adding text and images ------------------------------------------------------

    def insert_text(self) -> None:
        if self.data is None:
            return
        last = self._insert if self._insert and self._insert[0] == "text" else None
        dlg = TextDialog(self, *(last[1:] if last else ()))
        if not self._exec_text(dlg):
            return
        self._insert = ("text", dlg.text, dlg.size, dlg.color)
        self.view.set_insert_mode(True)
        self.statusBar().showMessage(_(
            "Drag a box for the text on the page (it wraps at its width), or click where "
            "it should start. Esc cancels."))

    def _exec_text(self, dlg: TextDialog) -> int:
        return dlg.exec()  # a hook for the tests

    def insert_image(self) -> None:
        if self.data is None:
            return
        start = str(self.path.parent) if self.path else ""
        path, _filter = QFileDialog.getOpenFileName(
            self, _("Add image"), start,
            _("Images") + " (*.png *.jpg *.jpeg *.gif *.bmp *.tif *.tiff *.webp *.pdf);;"
            + _("All files") + " (*)")
        if not path:
            return
        try:
            aspect = insert.image_aspect(path)
        except (SigningError, OSError, ValueError, ZeroDivisionError) as e:
            QMessageBox.critical(self, _("Add image"), str(e))
            return
        self._insert = ("image", path, aspect)
        self.view.set_insert_mode(True)
        self.statusBar().showMessage(_(
            "Drag a box for the image on the page, or click where its centre should be. "
            "Esc cancels."))

    def _insert_box(self, rect, what: tuple) -> tuple[float, float, float, float]:
        """The display box (x, y, width, height) for what is being added: the
        drawn rectangle (an image keeps its proportions inside it), or the
        natural size at a click; kept on the page."""
        pw, ph = self.doc.display_size(self.view.page)
        kind = what[0]
        if rect.width() < 5 or rect.height() < 5:  # a click
            if kind == "text":
                w, h = insert.text_size(what[1], what[2])
                x, y = rect.x(), rect.y()
            else:
                w = min(IMAGE_WIDTH, pw / 2)
                h = w / what[2]
                x, y = rect.x() - w / 2, rect.y() - h / 2
        else:
            x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
            if kind == "image":  # fitted in, centred
                aspect = what[2]
                fw, fh = (h * aspect, h) if w / h > aspect else (w, w / aspect)
                x, y, w, h = x + (w - fw) / 2, y + (h - fh) / 2, fw, fh
        w, h = min(w, pw), min(h, ph)
        return max(0.0, min(x, pw - w)), max(0.0, min(y, ph - h)), w, h

    def _insert_selected(self, page: int, rect) -> None:
        what = self._insert
        # the text dialog offers the last text again
        self._insert = what if what and what[0] == "text" else None
        if what is None or self.data is None:
            return
        self.statusBar().showMessage(_(HINT))
        x, y, w, h = self._insert_box(rect, what)
        if not self._confirm_breaking(_(
                "Adding to a page will invalidate the signature(s) {names}.\n\n"
                "Edit pages before signing. Continue anyway?")):
            return
        box = self.doc.rect_to_pdf(page, x, y, w, h)
        try:
            if what[0] == "text":
                new = insert.add_text(self.data, page, box, what[1], size=what[2], color=what[3],
                                      password=self.pdf_password)
                message = _("Added text to page {page}").format(page=page + 1)
            else:
                new = insert.add_image(self.data, page, box, what[1],
                                       password=self.pdf_password)
                message = _("Added an image to page {page}").format(page=page + 1)
        except (SigningError, ValueError, OSError) as e:  # forms.FormError, pages.PageError
            QMessageBox.critical(self, _("Add text") if what[0] == "text" else _("Add image"),
                                 str(e))
            return
        self.apply_edit(new, message, page)

    def save(self) -> bool:
        if self.data is None or self.path is None:
            return False
        try:
            _atomic_write(self.path, self.data)
        except (OSError, SigningError) as e:
            QMessageBox.critical(self, _("Save"), str(e))
            return False
        self.dirty = False
        self._update_title()
        self._update_page_actions()
        self.statusBar().showMessage(_("Saved {name}").format(name=self.path.name), 5000)
        return True

    def save_as(self) -> bool:
        if self.data is None:
            return False
        out, _filter = QFileDialog.getSaveFileName(self, _("Save PDF as"), str(self.path or ""),
                                                   _("PDF") + " (*.pdf)")
        if not out:
            return False
        self.path = Path(out)
        return self.save()

    def _maybe_save(self) -> bool:
        """Ask about unsaved edits. False means: cancel what you were doing."""
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, _("Unsaved changes"),
            _("Save the changes to {name}?").format(name=self.path.name),
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if answer == QMessageBox.Save:
            return self.save()
        return answer == QMessageBox.Discard

    def closeEvent(self, event) -> None:
        if self._maybe_save():
            event.accept()
        else:
            event.ignore()

    def _verify(self) -> str:
        """Validate the signatures and fill the list; returns a short summary."""
        self.sigs_list.clear()
        self.reports = []
        try:
            data, trust, password = self.data, self.config.trust, self.pdf_password
            # in a thread: a missing intermediate certificate may be downloaded
            self.reports = background.run(self, lambda ask: validation.verify_pdf(
                data, trust=trust, password=password))
        except Exception as e:  # never block opening a document
            self.sigs_list.addItem(_("Validation failed: {error}").format(error=e))
            return ""
        for r in self.reports:
            when = r.timestamp or r.signed_at
            signer = printable(r.signer.removeprefix("Common Name: "))
            if when:
                text = _("{field}: {status}\n   {signer}, {when}").format(
                    field=printable(r.field), status=_status_text(r.status), signer=signer,
                    when=f"{when:%Y-%m-%d %H:%M}")
            else:
                text = _("{field}: {status}\n   {signer}").format(
                    field=printable(r.field), status=_status_text(r.status), signer=signer)
            if r.changed_later and r.status not in (validation.INVALID,
                                                    validation.UNSUPPORTED):
                text += "\n   " + _("then changed (allowed): see tooltip")
            item = QListWidgetItem(_dot(STATUS_COLORS[r.status]), text)
            item.setData(Qt.UserRole, r.field)
            item.setToolTip("\n".join(map(printable, [r.coverage, *r.problems])))
            self.sigs_list.addItem(item)
        if not self.reports:
            return ""
        counts = {s: sum(r.status == s for r in self.reports)
                  for s in (validation.VALID, validation.UNTRUSTED, validation.INVALID,
                            validation.UNSUPPORTED)}
        parts = [_status_count(s, n) for s, n in counts.items() if n]
        n = len(self.reports)
        return ngettext("{n} signature: {counts}", "{n} signatures: {counts}", n).format(
            n=n, counts=", ".join(parts))

    def _signature_clicked(self, item: QListWidgetItem) -> None:
        f = self.info.field(item.data(Qt.UserRole)) if self.info else None
        if f is not None and f.page is not None:
            self.view.go_to(f.page)

    def show_signature_details(self, item: QListWidgetItem) -> None:
        r = next((r for r in self.reports if r.field == item.data(Qt.UserRole)), None)
        if r is None:
            return
        rows = [(_("Result"), _status_text(r.status).upper(), STATUS_COLORS[r.status]),
                # without the certificate's rows below, at least its names
                *([] if r.certificate else [(_("Signer"), r.signer), (_("Issuer"), r.issuer)]),
                (_("Signed at"), _when(r.signed_at)),
                (_("Trusted timestamp"), _when(r.timestamp)),
                (_("Covers"), r.coverage),
                (_("Reason"), r.reason), (_("Location"), r.location)]
        rows = [(_("Signature"), ""), *(row for row in rows if row[1])]
        rows += [(_("Problem"), p) for p in r.problems]
        rows = [(label, printable(value), *color) for label, value, *color in rows]
        pem_text = ""
        if r.certificate is not None:
            rows += [(_("Signer's certificate"), ""), *certinfo.details(r.certificate)]
            pem_text = certinfo.to_pem(r.certificate)
        details_dialog(self, _("Signature '{name}'").format(name=printable(r.field)),
                       rows, pem_text, printable(r.details, keep_newlines=True))

    def _fill_fields(self) -> None:
        self.fields_list.clear()
        for f in self.info.fields if self.info else []:
            state = _("signed") if f.filled else (_("LOCKED") if f.locked else _("empty"))
            where = (_("p. {page}").format(page=f.page + 1) if f.page is not None
                     else _("invisible"))
            item = QListWidgetItem(_("{name}\n   {state}, {where}").format(
                name=printable(f.name), state=state, where=where))
            item.setData(Qt.UserRole, f.name)
            if f.filled or f.locked or f.rect is None:
                item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
            self.fields_list.addItem(item)

    def _field_clicked(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.UserRole)
        f = self.info.field(name) if self.info else None
        if f is None:
            return
        if f.filled or f.locked:
            if f.page is not None:
                self.view.go_to(f.page)
            return
        self.view.select_field(name)

    def _field_activated(self, name: str) -> None:
        """A click on a field in the page: sign an empty one, show a signed
        one's validation."""
        f = self.info.field(name) if self.info else None
        if f is None or f.locked:
            return
        self.show_tab(HOME_TAB)  # signatures are on the Sign panel
        if f.filled:
            for i in range(self.sigs_list.count()):
                item = self.sigs_list.item(i)
                if item.data(Qt.UserRole) == name:
                    self.sigs_list.setCurrentItem(item)
                    self.show_signature_details(item)
            return
        self.view.select_field(name)
        if self._choose_certificate():
            self.sign()

    def _choose_certificate(self) -> bool:
        """Before signing a clicked field: with several certificates, ask
        which one (the current one preselected); with one, take it."""
        names = list(self.config.certs)
        if len(names) <= 1:
            return bool(names)
        current = self.cert_combo.currentData()
        labels = []
        for n in names:
            cert = certinfo.load(self.config.certs[n].certificate)
            labels.append(f"{n} ({certinfo.signer_name(cert)})" if cert else n)
        index = self._ask_certificate(labels, names.index(current) if current in names else 0)
        if index is None:
            return False
        self.cert_combo.setCurrentIndex(self.cert_combo.findData(names[index]))
        return True

    def _ask_certificate(self, labels: list[str], current: int) -> int | None:
        """Index of the chosen entry, or None (cancelled). A hook for tests."""
        label, ok = QInputDialog.getItem(self, _("Sign with"),
                                         _("Certificate for this signature:"),
                                         labels, current, False)
        return labels.index(label) if ok else None

    def _sync_page_spin(self, page: int) -> None:
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(page + 1)
        self.page_spin.blockSignals(False)

    def _update_target(self) -> None:
        target = None
        if self.view.selected_field:
            target = _("Prepared field '{name}'").format(name=self.view.selected_field)
        elif (pb := self.view.placement_box()) is not None:
            page, box = pb
            target = _("New signature on page {page}\n"
                       "at {x:.0f}, {y:.0f} pt, {width:.0f} × {height:.0f} pt").format(
                page=page + 1, x=box[0], y=box[1], width=box[2] - box[0],
                height=box[3] - box[1])
        for i in range(self.fields_list.count()):
            item = self.fields_list.item(i)
            item.setSelected(item.data(Qt.UserRole) == self.view.selected_field)
        self.target_label.setText(target or _("None yet: press Place signature…, or pick an "
                                                "empty field below."))
        # without a target, the button starts placing one; with one, it signs
        text = _("Sign…") if target else _("Place signature…")
        self.sign_act.setText(text)
        self.sign_act.setToolTip(
            _("Sign the document at the chosen place") if target
            else _("Then click on the page where the signature should go, or on an empty "
                   "signature field"))
        self.sign_btn.setText(text)
        self.sign_act.setEnabled(bool(self.doc and self.current_profile()))
        self.clear_act.setEnabled(bool(target) or self.view.place_mode)

    def _place_mode_changed(self, on: bool) -> None:
        self.sign_btn.setDown(on)
        self.clear_act.setEnabled(on or self.view.placement_box() is not None
                                  or self.view.selected_field is not None)
        self.statusBar().showMessage(_(PLACE_HINT) if on else _(HINT))

    # signing ------------------------------------------------------------------

    def sign(self) -> None:
        profile = self.current_profile()
        if not (self.doc and profile):
            return
        if self.ribbon.currentIndex() != HOME_TAB:  # the target and the Sign panel
            self.show_tab(HOME_TAB)
        field_name, page, box = self.view.selected_field, None, None
        if field_name is None:
            pb = self.view.placement_box()
            if pb is None:  # nothing placed yet: the next click places it
                self.view.set_place_mode(not self.view.place_mode)
                return
            page, box = pb

        try:
            check_target(self.info, field_name, page, box, allow_breaking=True)
        except SigningError as e:
            QMessageBox.critical(self, _("Cannot sign"), str(e))
            return
        if reason := self.info.blocker(field_name, new_visible=box is not None):
            if QMessageBox.warning(
                self, _("Existing signature will break"),
                _("Signing will invalidate an existing signature:\n{reason}\n\n"
                  "Sign anyway?").format(reason=reason),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return

        default = self.path.with_name(
            self.path.name if self.path.stem.endswith("-signed")
            else f"{self.path.stem}-signed.pdf")
        out, _filter = QFileDialog.getSaveFileName(self, _("Save signed PDF"), str(default),
                                                   _("PDF") + " (*.pdf)")
        if not out:
            return
        out = Path(out)

        data, pdf_password, trust = self.data, self.pdf_password, self.config.trust
        with_image, with_text = self.image_check.isChecked(), self.text_check.isChecked()

        def work(ask):  # in a thread: cards, KeePassXC and servers may take a while
            creds = secretstore.resolve_credentials(
                profile, lambda prompt: ask(lambda: self._ask_unbusy(prompt)))
            with open_signer(profile, creds) as signer:
                name = sign_pdf(
                    data, out, signer, profile,
                    field_name=field_name, page=page, box=box,
                    with_image=with_image, with_text=with_text,
                    pdf_password=pdf_password, allow_breaking=True, trust=trust,
                )
                return name, certinfo.to_pem(signer.signing_cert)

        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        if profile.type in HARDWARE_TYPES:  # may wait for a touch or a PIN pad
            self.statusBar().showMessage(_(TOKEN_HINT))
            systemstore.parent_window = int(self.winId())
        else:
            self.statusBar().showMessage(_("Signing…"))
        try:
            name, pem_text = background.run(self, work)
        except secretstore.Cancelled:
            self.statusBar().showMessage(_(HINT))
            return
        except (SigningError, SigningBlocked, secretstore.SecretError, ValueError, OSError) as e:
            QGuiApplication.restoreOverrideCursor()
            self.statusBar().showMessage(_(HINT))
            QMessageBox.critical(self, _("Signing failed"), str(e))
            return
        finally:
            if QGuiApplication.overrideCursor() is not None:
                QGuiApplication.restoreOverrideCursor()

        self._remember_certificate(profile.name, pem_text)
        self.open(out, self.pdf_password, page=self.view.page)
        self.statusBar().showMessage(
            _("Signed '{field}' → {file}. You can add further signatures.").format(
                field=name, file=out.name), 15000)

    def _remember_certificate(self, name: str, pem_text: str) -> None:
        """Keep the profile's stored certificate current (e.g. a renewed card)."""
        p = self.config.certs.get(name)
        if p is None or p.certificate == pem_text:
            return
        p.certificate = pem_text
        if self.config.path is None:  # not from a file (tests): nothing to update
            return self._cert_changed()
        try:
            self.config.save()
        except OSError:  # only details for display; signing worked
            pass
        self._cert_changed()

    def _ask_unbusy(self, prompt: str) -> str | None:
        QGuiApplication.restoreOverrideCursor()
        try:
            return ask_secret(self, prompt)
        finally:
            QGuiApplication.setOverrideCursor(Qt.WaitCursor)


ICON = Path(__file__).with_name("icon.png")
HOMEPAGE = "https://github.com/linuxrrze/slim-pdf"
DOCS_URL = HOMEPAGE + "/blob/main/docs/gui.md"
IMAGE_WIDTH = 150  # points: an image added with a click
STAMP_PREVIEW_PX = 240  # height of the placed stamp's preview, sharp enough when zoomed
HARDWARE_TYPES = ("pkcs11", "system")
TOKEN_HINT = N_("Signing… If your card reader asks for the PIN or your security key "
                "blinks, confirm there.")


def version() -> str:
    try:
        return metadata.version("slim-pdf")
    except metadata.PackageNotFoundError:  # run from a source tree
        return _("unknown")


def about_text() -> str:
    """The About box: what this is, its version, and what it is built on."""
    def dist(name):
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            return "?"

    libs = [("pyHanko", dist("pyhanko")), ("pypdfium2", dist("pypdfium2")),
            ("Qt", qVersion()), ("PySide6", dist("PySide6")),
            ("Python", platform.python_version())]
    return "".join([
        "<h3>{}</h3>".format(html.escape(_("Slim PDF {version}").format(version=version()))),
        "<p>{}</p>".format(html.escape(_(
            "Sign PDFs with real certificates (PAdES), check signatures, fill in forms, "
            "add text and images, and edit pages."))),
        "<p>{}</p>".format(html.escape(_("Built with: {libraries}").format(
            libraries=", ".join(f"{name} {v}" for name, v in libs)))),
        "<p>{}</p>".format(html.escape(_("Icons: Tabler Icons (MIT license)"))),
        '<p><a href="{url}">{url}</a></p>'.format(url=HOMEPAGE),
    ])


def setup_language(app: QApplication, configured: str | None) -> str:
    """Our catalog plus Qt's own (OK/Cancel/Yes/No, file dialogs)."""
    lang = i18n.setup(configured, QLocale.system().uiLanguages() + i18n.system_languages())
    old = getattr(app, "_slim_pdf_qt_translator", None)
    if old is not None:
        app.removeTranslator(old)
    if lang != "en":
        translator = QTranslator(app)
        if translator.load(f"qtbase_{lang}", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
            app.installTranslator(translator)
            app._slim_pdf_qt_translator = translator
    return lang


def main(argv: list[str] | None = None, config_path: Path | None = None) -> int:
    """GUI entry point. ``--smoke-test`` builds the window (and opens the
    file, if given), then exits 0 without waiting for the user; the
    installer builds use it to check that the bundled app starts."""
    argv = sys.argv[1:] if argv is None else argv
    app = QApplication.instance() or QApplication([sys.argv[0], *argv])
    app.setApplicationName("slim-pdf")
    if ICON.exists():
        app.setWindowIcon(QIcon(str(ICON)))
    try:
        config = load_config(config_path)
    except (ValueError, OSError) as e:  # tomllib.TOMLDecodeError is a ValueError
        setup_language(app, None)
        QMessageBox.critical(None, _("Slim PDF"),
                             _("Cannot read the configuration:\n{error}").format(error=e))
        return 1
    setup_language(app, config.language)
    try:
        dog = watchdog.Watchdog(app)
    except OSError:  # no log folder: run without
        dog = None
    win = MainWindow(config)
    win.show()
    files = [a for a in argv if not a.startswith("-")]
    opened = win.open(Path(files[0])) if files else True
    try:
        if "--smoke-test" in argv:
            app.processEvents()
            win.close()
            return 0 if opened and (not files or win.doc is not None) else 1
        return app.exec()
    finally:
        if dog is not None:
            dog.stop()


if __name__ == "__main__":
    sys.exit(main())
