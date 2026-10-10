"""Main window: open a PDF, pick a field or click to place, sign, repeat."""

from __future__ import annotations

import html
import sys
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QLocale, Qt, QTranslator
from PySide6.QtGui import QAction, QColor, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import i18n, pages, secretstore, validation
from ..config import Config, load_config
from ..i18n import N_, _, ngettext
from ..signing import (
    PasswordError,
    SigningBlocked,
    SigningError,
    _atomic_write,
    check_target,
    image_aspect,
    inspect_pdf,
    open_signer,
    printable,
    sign_pdf,
)
from .certdialog import CertManagerDialog
from .settings import SettingsDialog
from .dialogs import ask_secret
from .document import PdfDocument, signature_pixmap
from .pageview import PageView
from .pagetools import MergeDialog, MergeInput, PagesPanel, SplitDialog

STATUS_COLORS = {validation.VALID: "#2e7d32", validation.UNTRUSTED: "#ef8c00",
                 validation.INVALID: "#c62828"}


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


HINT = N_("Click on the page to place a signature, drag to move, drag the corner or "
          "Ctrl+wheel to resize. Click a dashed field to sign a prepared field.")


def _status_text(status: str) -> str:
    """The word shown for a validation status code."""
    return {validation.VALID: _("valid"), validation.UNTRUSTED: _("untrusted"),
            validation.INVALID: _("invalid")}.get(status, status)


def _status_count(status: str, n: int) -> str:
    """'2 valid' etc. for the status bar summary."""
    if status == validation.VALID:
        return ngettext("{n} valid", "{n} valid", n).format(n=n)
    if status == validation.UNTRUSTED:
        return ngettext("{n} untrusted", "{n} untrusted", n).format(n=n)
    return ngettext("{n} invalid", "{n} invalid", n).format(n=n)


class MainWindow(QMainWindow):
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.doc: PdfDocument | None = None
        self.path: Path | None = None
        self.pdf_password: str | None = None
        self.info = None
        # The document is edited in memory (page tools); self.path is where it
        # came from or was last saved to.
        self.data: bytes | None = None
        self.dirty = False
        self.undo_stack: list[bytes] = []

        self.setWindowTitle(_("PDF Signer"))
        self.resize(1100, 900)
        self.view = PageView()
        self.view.targetChanged.connect(self._update_target)
        self.view.pageChanged.connect(self._sync_page_spin)
        self.view.cropSelected.connect(self._crop_selected)
        self.setCentralWidget(self.view)

        self._build_toolbar()
        self._build_pages_toolbar()
        self._build_sign_panel()
        self._build_pages_panel()
        self.statusBar().showMessage(_(HINT))
        self._reload_certs()
        self._update_target()

    # toolbar ----------------------------------------------------------------

    def _build_toolbar(self) -> None:
        tb = QToolBar(_("Main"))
        tb.setMovable(False)
        self.addToolBar(tb)
        self.main_toolbar = tb

        open_act = QAction(_("Open…"), self, shortcut=QKeySequence.Open, triggered=self.open_dialog)
        tb.addAction(open_act)
        tb.addSeparator()

        tb.addAction(QAction(_("‹ Prev"), self, shortcut=QKeySequence.MoveToPreviousPage,
                             triggered=lambda: self.view.go_to(self.view.page - 1)))
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.valueChanged.connect(lambda v: self.view.go_to(v - 1))
        tb.addWidget(self.page_spin)
        self.page_count = QLabel(" / 0 ")
        tb.addWidget(self.page_count)
        tb.addAction(QAction(_("Next ›"), self, shortcut=QKeySequence.MoveToNextPage,
                             triggered=lambda: self.view.go_to(self.view.page + 1)))
        tb.addSeparator()
        tb.addAction(QAction("−", self, shortcut=QKeySequence.ZoomOut,
                             triggered=lambda: self.view.set_zoom(self.view.zoom / 1.2)))
        tb.addAction(QAction(_("Fit"), self, triggered=lambda: self.view.set_zoom(None)))
        tb.addAction(QAction("+", self, shortcut=QKeySequence.ZoomIn,
                             triggered=lambda: self.view.set_zoom(self.view.zoom * 1.2)))
        tb.addSeparator()
        tb.addAction(QAction(_("Clear placement"), self, shortcut=Qt.Key_Escape,
                             triggered=self._escape))
        tb.addSeparator()
        tb.addAction(QAction(_("Settings…"), self, shortcut=QKeySequence.Preferences,
                             triggered=self.settings_dialog))

    def _build_pages_toolbar(self) -> None:
        tb = QToolBar(_("Pages"))
        tb.setMovable(False)
        self.addToolBar(tb)
        self.pages_toolbar = tb

        def act(text, slot, shortcut=None, tip=""):
            a = QAction(text, self, triggered=slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.setToolTip(tip or text)
            tb.addAction(a)
            self.page_actions.append(a)
            return a

        self.page_actions: list[QAction] = []
        self.save_act = act(_("Save"), self.save, tip=_("Save the edited document"))
        self.save_as_act = act(_("Save as…"), self.save_as, "Ctrl+Shift+S")
        self.undo_act = act(_("Undo"), self.undo, QKeySequence.Undo)
        tb.addSeparator()
        act(_("Rotate left"), lambda: self.rotate_pages(-90), "Ctrl+L",
            _("Rotate the selected pages (or the current page) counter-clockwise"))
        act(_("Rotate right"), lambda: self.rotate_pages(90), "Ctrl+R",
            _("Rotate the selected pages (or the current page) clockwise"))
        self.crop_act = act(_("Crop"), self._toggle_crop, None,
                            _("Drag a rectangle on the page to crop to it"))
        self.crop_act.setCheckable(True)
        act(_("Uncrop"), self.reset_crop, None, _("Show the whole page again"))
        tb.addSeparator()
        act(_("Split…"), self.split_dialog)
        act(_("Merge…"), self.merge_dialog)
        self._update_page_actions()

    def _build_pages_panel(self) -> None:
        self.thumbs = PagesPanel()
        self.thumbs.pageActivated.connect(self.view.go_to)
        self.view.pageChanged.connect(self.thumbs.show_page)
        dock = QDockWidget(_("Pages"))
        dock.setWidget(self.thumbs)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self.resizeDocks([dock], [self.thumbs.minimumWidth() + 20], Qt.Horizontal)
        self.pages_dock = dock

    def _escape(self) -> None:
        if self.view.crop_mode:
            self.crop_act.setChecked(False)
            self.view.set_crop_mode(False)
            self.statusBar().showMessage(_(HINT))
        else:
            self.view.clear_target()

    def _build_sign_panel(self) -> None:
        panel = QWidget()
        lay = QVBoxLayout(panel)

        lay.addWidget(QLabel("<b>{}</b>".format(_("Certificate"))))
        row = QHBoxLayout()
        self.cert_combo = QComboBox()
        self.cert_combo.currentIndexChanged.connect(self._cert_changed)
        row.addWidget(self.cert_combo, 1)
        manage = QPushButton(_("Manage…"))
        manage.clicked.connect(self.manage_certs)
        row.addWidget(manage)
        lay.addLayout(row)

        row = QHBoxLayout()
        self.image_check = QCheckBox(_("Signature image"))
        self.image_check.setChecked(True)
        self.image_check.toggled.connect(self._cert_changed)
        self.text_check = QCheckBox(_("Text"))
        self.text_check.setChecked(True)
        row.addWidget(self.image_check)
        row.addWidget(self.text_check)
        row.addStretch()
        lay.addLayout(row)

        lay.addSpacing(8)
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

        dock = QDockWidget(_("Sign"))
        dock.setWidget(panel)
        dock.setFeatures(QDockWidget.DockWidgetMovable)
        dock.setMinimumWidth(280)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

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
        if p and p.image_path and self.image_check.isChecked():
            pm = signature_pixmap(p.image_path)
            if not pm.isNull():
                pixmap, aspect = pm, image_aspect(p.image_path)
        self.view.set_signature_appearance(pixmap, aspect)
        self._update_target()

    def settings_dialog(self) -> None:
        dlg = SettingsDialog(self.config, self)
        if not dlg.exec():
            return
        trust_changed = dlg.config.trust != self.config.trust
        self.config = dlg.config
        if trust_changed and self.data is not None:
            self.statusBar().showMessage(self._verify() or _(HINT))
        if dlg.language_changed:
            QMessageBox.information(self, _("Settings"),
                                    _("The new language is used after restarting PDF Signer."))

    def manage_certs(self) -> None:
        dlg = CertManagerDialog(self.config, self)
        if dlg.exec():
            self.config = dlg.config
            self._reload_certs()

    # documents ----------------------------------------------------------------

    def open_dialog(self) -> None:
        if not self._maybe_save():
            return
        start = str(self.path.parent) if self.path else ""
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
        self.undo_stack = []
        self._show(data, info, page, dirty=False)
        return True

    def _show(self, data: bytes, info, page: int, *, dirty: bool) -> None:
        """Display ``data`` (already inspected as ``info``)."""
        if self.doc is not None:
            self.doc.close()
        self.data, self.info, self.dirty = data, info, dirty
        self.doc = PdfDocument(data, self.pdf_password)
        page = min(page, len(self.doc) - 1)
        self.view.set_document(self.doc, info.fields, page)
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
        name = self.path.name if self.path else _("PDF Signer")
        self.setWindowTitle(_("{modified}{name} – PDF Signer").format(
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

    def apply_edit(self, new_data: bytes, message: str) -> None:
        self.undo_stack.append(self.data)
        self._show(new_data, inspect_pdf(new_data, self.pdf_password), self.view.page,
                   dirty=True)
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

    def split_dialog(self) -> None:
        if self.data is None:
            return
        dlg = SplitDialog(len(self.doc), self.path, self)
        if not dlg.exec():
            return
        names = pages.signature_names(self.data, self.pdf_password)
        if names and QMessageBox.warning(
                self, _("Split"), _("The split files will not contain the signature(s) "
                                    "{names}. Continue?").format(names=", ".join(names)),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            parts = pages.split(self.data, dlg.result_parts, self.pdf_password)
            for path, part in zip(dlg.result_paths, parts, strict=True):
                _atomic_write(path, part)
        except (OSError, SigningError, pages.PageError) as e:
            QMessageBox.critical(self, _("Split"), str(e))
            return
        n = len(dlg.result_paths)
        paths = "\n".join(str(p) for p in dlg.result_paths)
        QMessageBox.information(self, _("Split"), ngettext(
            "Wrote {n} file:\n{paths}", "Wrote {n} files:\n{paths}", n).format(n=n, paths=paths))

    def merge_dialog(self) -> None:
        first = None
        if self.data is not None:
            first = MergeInput(_("(current document)"), self.data, self.pdf_password, len(self.doc))
        dlg = MergeDialog(first, self)
        if not dlg.exec():
            return
        signed = [m.label for m in dlg.inputs if pages.signature_names(m.data, m.password)]
        if signed and QMessageBox.warning(
                self, _("Merge"), _("Signatures in {names} will not be carried "
                                    "over. Continue?").format(names=", ".join(signed)),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        start = str(self.path.with_name(f"{self.path.stem}-merged.pdf")) if self.path else ""
        out, _filter = QFileDialog.getSaveFileName(self, _("Save merged PDF"), start,
                                                   _("PDF") + " (*.pdf)")
        if not out:
            return
        try:
            _atomic_write(Path(out), pages.merge(dlg.selection()))
        except (OSError, SigningError, pages.PageError) as e:
            QMessageBox.critical(self, _("Merge"), str(e))
            return
        if self._maybe_save():
            self.open(Path(out))

    # saving ---------------------------------------------------------------------

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

    def _arrange_toolbars(self) -> None:
        """Page tools go to a second row when both toolbars don't fit
        (longer labels in German, narrow windows)."""
        need = (self.main_toolbar.sizeHint().width()
                + self.pages_toolbar.sizeHint().width() + 16)
        wrapped = self.toolBarBreak(self.pages_toolbar)
        if need > self.width() and not wrapped:
            self.insertToolBarBreak(self.pages_toolbar)
        elif need <= self.width() and wrapped:
            self.removeToolBarBreak(self.pages_toolbar)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._arrange_toolbars()

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
            self.reports = validation.verify_pdf(self.data, trust=self.config.trust,
                                                 password=self.pdf_password)
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
            if r.changed_later and r.status != validation.INVALID:
                text += "\n   " + _("then changed (allowed): see tooltip")
            item = QListWidgetItem(_dot(STATUS_COLORS[r.status]), text)
            item.setData(Qt.UserRole, r.field)
            item.setToolTip("\n".join(map(printable, [r.coverage, *r.problems])))
            self.sigs_list.addItem(item)
        if not self.reports:
            return ""
        counts = {s: sum(r.status == s for r in self.reports)
                  for s in (validation.VALID, validation.UNTRUSTED, validation.INVALID)}
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
        box = QMessageBox(self)
        box.setWindowTitle(_("Signature '{name}'").format(name=printable(r.field)))
        box.setIcon(QMessageBox.Information if r.ok else QMessageBox.Warning)
        lines = ["<b>{}</b>".format(html.escape(_status_text(r.status).upper())),
                 _("Signer: {signer}").format(signer=r.signer),
                 _("Issuer: {issuer}").format(issuer=r.issuer),
                 _("Covers: {coverage}").format(coverage=r.coverage),
                 *(_("Problem: {problem}").format(problem=p) for p in r.problems)]
        lines = [lines[0], *(html.escape(printable(line)) for line in lines[1:])]
        box.setText("<br>".join(lines))
        box.setDetailedText(printable(r.details, keep_newlines=True))
        box.exec()

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
        self.target_label.setText(target or _("Click on the page to place a signature, "
                                                "or pick an empty field."))
        self.sign_act.setEnabled(bool(self.doc and target and self.current_profile()))

    # signing ------------------------------------------------------------------

    def sign(self) -> None:
        profile = self.current_profile()
        if not (self.doc and profile):
            return
        field_name, page, box = self.view.selected_field, None, None
        if field_name is None:
            pb = self.view.placement_box()
            if pb is None:
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

        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            creds = secretstore.resolve_credentials(
                profile, lambda prompt: self._ask_unbusy(prompt))
            with open_signer(profile, creds) as signer:
                name = sign_pdf(
                    self.data, out, signer, profile,
                    field_name=field_name, page=page, box=box,
                    with_image=self.image_check.isChecked(),
                    with_text=self.text_check.isChecked(),
                    pdf_password=self.pdf_password, allow_breaking=True,
                    trust=self.config.trust,
                )
        except secretstore.Cancelled:
            return
        except (SigningError, SigningBlocked, secretstore.SecretError, ValueError, OSError) as e:
            QMessageBox.critical(self, _("Signing failed"), str(e))
            return
        finally:
            QGuiApplication.restoreOverrideCursor()

        self.open(out, self.pdf_password, page=self.view.page)
        self.statusBar().showMessage(
            _("Signed '{field}' → {file}. You can add further signatures.").format(
                field=name, file=out.name), 15000)

    def _ask_unbusy(self, prompt: str) -> str | None:
        QGuiApplication.restoreOverrideCursor()
        try:
            return ask_secret(self, prompt)
        finally:
            QGuiApplication.setOverrideCursor(Qt.WaitCursor)


ICON = Path(__file__).with_name("icon.png")


def setup_language(app: QApplication, configured: str | None) -> str:
    """Our catalog plus Qt's own (OK/Cancel/Yes/No, file dialogs)."""
    lang = i18n.setup(configured, QLocale.system().uiLanguages() + i18n.system_languages())
    old = getattr(app, "_pdfsigner_qt_translator", None)
    if old is not None:
        app.removeTranslator(old)
    if lang != "en":
        translator = QTranslator(app)
        if translator.load(f"qtbase_{lang}", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
            app.installTranslator(translator)
            app._pdfsigner_qt_translator = translator
    return lang


def main(argv: list[str] | None = None, config_path: Path | None = None) -> int:
    """GUI entry point. ``--smoke-test`` builds the window (and opens the
    file, if given), then exits 0 without waiting for the user; the
    installer builds use it to check that the bundled app starts."""
    argv = sys.argv[1:] if argv is None else argv
    app = QApplication.instance() or QApplication([sys.argv[0], *argv])
    app.setApplicationName("pdfsigner")
    if ICON.exists():
        app.setWindowIcon(QIcon(str(ICON)))
    try:
        config = load_config(config_path)
    except (ValueError, OSError) as e:  # tomllib.TOMLDecodeError is a ValueError
        setup_language(app, None)
        QMessageBox.critical(None, _("PDF Signer"),
                             _("Cannot read the configuration:\n{error}").format(error=e))
        return 1
    setup_language(app, config.language)
    win = MainWindow(config)
    win.show()
    files = [a for a in argv if not a.startswith("-")]
    opened = win.open(Path(files[0])) if files else True
    if "--smoke-test" in argv:
        app.processEvents()
        win.close()
        return 0 if opened and (not files or win.doc is not None) else 1
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
