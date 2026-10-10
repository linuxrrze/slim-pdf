"""Main window: open a PDF, pick a field or click to place, sign, repeat."""

from __future__ import annotations

import html
import sys
from pathlib import Path

from PySide6.QtCore import Qt
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

from .. import pages, secretstore, validation
from ..config import Config, load_config
from ..signing import (
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
from .dialogs import ask_secret
from .document import PdfDocument, signature_pixmap
from .pageview import PageView
from .pagetools import MergeDialog, MergeInput, PagesPanel, SplitDialog, page_label

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


HINT = ("Click on the page to place a signature, drag to move, drag the corner or "
        "Ctrl+wheel to resize. Click a dashed field to sign a prepared field.")


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

        self.setWindowTitle("PDF Signer")
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
        self.statusBar().showMessage(HINT)
        self._reload_certs()
        self._update_target()

    # toolbar ----------------------------------------------------------------

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        open_act = QAction("Open…", self, shortcut=QKeySequence.Open, triggered=self.open_dialog)
        tb.addAction(open_act)
        tb.addSeparator()

        tb.addAction(QAction("‹ Prev", self, shortcut=QKeySequence.MoveToPreviousPage,
                             triggered=lambda: self.view.go_to(self.view.page - 1)))
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.valueChanged.connect(lambda v: self.view.go_to(v - 1))
        tb.addWidget(self.page_spin)
        self.page_count = QLabel(" / 0 ")
        tb.addWidget(self.page_count)
        tb.addAction(QAction("Next ›", self, shortcut=QKeySequence.MoveToNextPage,
                             triggered=lambda: self.view.go_to(self.view.page + 1)))
        tb.addSeparator()
        tb.addAction(QAction("−", self, shortcut=QKeySequence.ZoomOut,
                             triggered=lambda: self.view.set_zoom(self.view.zoom / 1.2)))
        tb.addAction(QAction("Fit", self, triggered=lambda: self.view.set_zoom(None)))
        tb.addAction(QAction("+", self, shortcut=QKeySequence.ZoomIn,
                             triggered=lambda: self.view.set_zoom(self.view.zoom * 1.2)))
        tb.addSeparator()
        tb.addAction(QAction("Clear placement", self, shortcut=Qt.Key_Escape,
                             triggered=self._escape))

    def _build_pages_toolbar(self) -> None:
        tb = QToolBar("Pages")
        tb.setMovable(False)
        self.addToolBar(tb)

        def act(text, slot, shortcut=None, tip=""):
            a = QAction(text, self, triggered=slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.setToolTip(tip or text)
            tb.addAction(a)
            self.page_actions.append(a)
            return a

        self.page_actions: list[QAction] = []
        self.save_act = act("Save", self.save, tip="Save the edited document")
        self.save_as_act = act("Save as…", self.save_as, "Ctrl+Shift+S")
        self.undo_act = act("Undo", self.undo, QKeySequence.Undo)
        tb.addSeparator()
        act("Rotate left", lambda: self.rotate_pages(-90), "Ctrl+L",
            "Rotate the selected pages (or the current page) counter-clockwise")
        act("Rotate right", lambda: self.rotate_pages(90), "Ctrl+R",
            "Rotate the selected pages (or the current page) clockwise")
        self.crop_act = act("Crop", self._toggle_crop, None,
                            "Drag a rectangle on the page to crop to it")
        self.crop_act.setCheckable(True)
        act("Uncrop", self.reset_crop, None, "Show the whole page again")
        tb.addSeparator()
        act("Split…", self.split_dialog)
        act("Merge…", self.merge_dialog)
        self._update_page_actions()

    def _build_pages_panel(self) -> None:
        self.thumbs = PagesPanel()
        self.thumbs.pageActivated.connect(self.view.go_to)
        self.view.pageChanged.connect(self.thumbs.show_page)
        dock = QDockWidget("Pages")
        dock.setWidget(self.thumbs)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self.resizeDocks([dock], [self.thumbs.minimumWidth() + 20], Qt.Horizontal)
        self.pages_dock = dock

    def _escape(self) -> None:
        if self.view.crop_mode:
            self.crop_act.setChecked(False)
            self.view.set_crop_mode(False)
            self.statusBar().showMessage(HINT)
        else:
            self.view.clear_target()

    def _build_sign_panel(self) -> None:
        panel = QWidget()
        lay = QVBoxLayout(panel)

        lay.addWidget(QLabel("<b>Certificate</b>"))
        row = QHBoxLayout()
        self.cert_combo = QComboBox()
        self.cert_combo.currentIndexChanged.connect(self._cert_changed)
        row.addWidget(self.cert_combo, 1)
        manage = QPushButton("Manage…")
        manage.clicked.connect(self.manage_certs)
        row.addWidget(manage)
        lay.addLayout(row)

        row = QHBoxLayout()
        self.image_check = QCheckBox("Signature image")
        self.image_check.setChecked(True)
        self.image_check.toggled.connect(self._cert_changed)
        self.text_check = QCheckBox("Text")
        self.text_check.setChecked(True)
        row.addWidget(self.image_check)
        row.addWidget(self.text_check)
        row.addStretch()
        lay.addLayout(row)

        lay.addSpacing(8)
        lay.addWidget(QLabel("<b>Target</b>"))
        self.target_label = QLabel()
        self.target_label.setWordWrap(True)
        lay.addWidget(self.target_label)

        self.sign_act = QAction("Sign…", self, shortcut=QKeySequence("Ctrl+S"),
                                triggered=self.sign)
        self.addAction(self.sign_act)
        self.sign_btn = QPushButton("Sign…")
        self.sign_btn.setDefault(True)
        self.sign_btn.setMinimumHeight(34)
        self.sign_btn.clicked.connect(self.sign_act.trigger)
        self.sign_act.changed.connect(
            lambda: self.sign_btn.setEnabled(self.sign_act.isEnabled()))
        lay.addWidget(self.sign_btn)

        lay.addSpacing(8)
        lay.addWidget(QLabel("<b>Signature fields</b>"))
        self.fields_list = QListWidget()
        self.fields_list.itemClicked.connect(self._field_clicked)
        lay.addWidget(self.fields_list, 1)

        lay.addSpacing(8)
        lay.addWidget(QLabel("<b>Signatures</b>"))
        self.sigs_list = QListWidget()
        self.sigs_list.setToolTip("Double-click for the full validation report")
        self.sigs_list.itemClicked.connect(self._signature_clicked)
        self.sigs_list.itemDoubleClicked.connect(self.show_signature_details)
        lay.addWidget(self.sigs_list, 1)
        self.reports: list[validation.SignatureReport] = []

        dock = QDockWidget("Sign")
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

    def _cert_changed(self, *_) -> None:
        p = self.current_profile()
        pixmap, aspect = None, 3.0
        if p and p.image_path and self.image_check.isChecked():
            pm = signature_pixmap(p.image_path)
            if not pm.isNull():
                pixmap, aspect = pm, image_aspect(p.image_path)
        self.view.set_signature_appearance(pixmap, aspect)
        self._update_target()

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
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", start, "PDF (*.pdf)")
        if path:
            self.open(Path(path))

    def open(self, path: Path, password: str | None = None, *, page: int = 0) -> bool:
        try:
            data = Path(path).read_bytes()
            info = inspect_pdf(data, password)
        except SigningError as e:
            if "password" in str(e):
                pw = ask_secret(self, f"Password for {path.name}")
                return pw is not None and self.open(path, pw, page=page)
            QMessageBox.critical(self, "Open", str(e))
            return False
        except Exception as e:  # malformed PDFs raise all sorts of things
            QMessageBox.critical(self, "Open", f"Cannot open {path}:\n{e}")
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
        self.page_count.setText(f" / {len(self.doc)} ")
        self._fill_fields()
        self.thumbs.set_document(self.doc, page)
        self._update_title()
        message = self._verify() or HINT
        if info.certification is not None:
            message = f"Document is certified ({info.certification.name}). " + message
        self.statusBar().showMessage(message)
        self._update_page_actions()

    def _update_title(self) -> None:
        name = self.path.name if self.path else "PDF Signer"
        self.setWindowTitle(f"{'*' if self.dirty else ''}{name} – PDF Signer")

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

    def _confirm_breaking(self, what: str) -> bool:
        names = pages.signature_names(self.data, self.pdf_password)
        if not names:
            return True
        return QMessageBox.warning(
            self, "Existing signatures",
            f"{what} will invalidate the signature(s) {', '.join(names)}.\n\n"
            "Edit pages before signing. Continue anyway?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def apply_edit(self, new_data: bytes, message: str) -> None:
        self.undo_stack.append(self.data)
        self._show(new_data, inspect_pdf(new_data, self.pdf_password), self.view.page,
                   dirty=True)
        self.statusBar().showMessage(message + " (not saved yet)", 10000)

    def undo(self) -> None:
        if not self.undo_stack:
            return
        data = self.undo_stack.pop()
        self._show(data, inspect_pdf(data, self.pdf_password), self.view.page,
                   dirty=bool(self.undo_stack))
        self.statusBar().showMessage("Undone", 5000)

    def rotate_pages(self, angle: int) -> None:
        if self.data is None:
            return
        target = self.target_pages()
        if not self._confirm_breaking("Rotating pages"):
            return
        self.apply_edit(pages.rotate(self.data, target, angle, self.pdf_password),
                        f"Rotated {page_label(target)}")

    def _toggle_crop(self) -> None:
        on = self.crop_act.isChecked() and self.data is not None
        self.view.set_crop_mode(on)
        if on:
            self.statusBar().showMessage(
                "Drag a rectangle around the area to keep. Esc cancels.")

    def _crop_selected(self, page: int, rect) -> None:
        self.crop_act.setChecked(False)
        self.view.set_crop_mode(False)
        box = self.doc.rect_to_pdf(page, rect.x(), rect.y(), rect.width(), rect.height())
        selected = self.thumbs.selected_pages()
        choices = [f"This page ({page + 1})", "All pages"]
        if len(selected) > 1:
            choices.insert(1, f"Selected pages ({len(selected)})")
        choice, ok = QInputDialog.getItem(self, "Crop", "Apply to:", choices, 0, False)
        if not ok:
            return
        target = ([page] if choice == choices[0] else
                  list(range(len(self.doc))) if choice == "All pages" else selected)
        if not self._confirm_breaking("Cropping pages"):
            return
        try:
            new = pages.crop(self.data, target, box=box, password=self.pdf_password)
        except (pages.PageError, SigningError) as e:
            QMessageBox.critical(self, "Crop", str(e))
            return
        self.apply_edit(new, f"Cropped {page_label(target)}")

    def reset_crop(self) -> None:
        if self.data is None:
            return
        target = self.target_pages()
        if not self._confirm_breaking("Uncropping pages"):
            return
        self.apply_edit(pages.crop(self.data, target, reset=True, password=self.pdf_password),
                        f"Uncropped {page_label(target)}")

    def split_dialog(self) -> None:
        if self.data is None:
            return
        dlg = SplitDialog(len(self.doc), self.path, self)
        if not dlg.exec():
            return
        names = pages.signature_names(self.data, self.pdf_password)
        if names and QMessageBox.warning(
                self, "Split", f"The split files will not contain the signature(s) "
                f"{', '.join(names)}. Continue?", QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            parts = pages.split(self.data, dlg.result_parts, self.pdf_password)
            for path, part in zip(dlg.result_paths, parts, strict=True):
                _atomic_write(path, part)
        except (OSError, SigningError, pages.PageError) as e:
            QMessageBox.critical(self, "Split", str(e))
            return
        QMessageBox.information(self, "Split", f"Wrote {len(dlg.result_paths)} files:\n"
                                + "\n".join(str(p) for p in dlg.result_paths))

    def merge_dialog(self) -> None:
        first = None
        if self.data is not None:
            first = MergeInput("(current document)", self.data, self.pdf_password, len(self.doc))
        dlg = MergeDialog(first, self)
        if not dlg.exec():
            return
        signed = [m.label for m in dlg.inputs if pages.signature_names(m.data, m.password)]
        if signed and QMessageBox.warning(
                self, "Merge", f"Signatures in {', '.join(signed)} will not be carried "
                "over. Continue?", QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        start = str(self.path.with_name(f"{self.path.stem}-merged.pdf")) if self.path else ""
        out, _ = QFileDialog.getSaveFileName(self, "Save merged PDF", start, "PDF (*.pdf)")
        if not out:
            return
        try:
            _atomic_write(Path(out), pages.merge(dlg.selection()))
        except (OSError, SigningError, pages.PageError) as e:
            QMessageBox.critical(self, "Merge", str(e))
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
            QMessageBox.critical(self, "Save", str(e))
            return False
        self.dirty = False
        self._update_title()
        self._update_page_actions()
        self.statusBar().showMessage(f"Saved {self.path.name}", 5000)
        return True

    def save_as(self) -> bool:
        if self.data is None:
            return False
        out, _ = QFileDialog.getSaveFileName(self, "Save PDF as", str(self.path or ""),
                                             "PDF (*.pdf)")
        if not out:
            return False
        self.path = Path(out)
        return self.save()

    def _maybe_save(self) -> bool:
        """Ask about unsaved edits. False means: cancel what you were doing."""
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes", f"Save the changes to {self.path.name}?",
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
            self.reports = validation.verify_pdf(self.data, trust=self.config.trust,
                                                 password=self.pdf_password)
        except Exception as e:  # never block opening a document
            self.sigs_list.addItem(f"Validation failed: {e}")
            return ""
        for r in self.reports:
            when = r.timestamp or r.signed_at
            signer = printable(r.signer.removeprefix("Common Name: "))
            text = f"{printable(r.field)}: {r.status}\n   {signer}"
            if when:
                text += f", {when:%Y-%m-%d %H:%M}"
            item = QListWidgetItem(_dot(STATUS_COLORS[r.status]), text)
            item.setData(Qt.UserRole, r.field)
            item.setToolTip("\n".join(map(printable, [r.coverage, *r.problems])))
            self.sigs_list.addItem(item)
        if not self.reports:
            return ""
        counts = {s: sum(r.status == s for r in self.reports)
                  for s in (validation.VALID, validation.UNTRUSTED, validation.INVALID)}
        parts = [f"{n} {s}" for s, n in counts.items() if n]
        n = len(self.reports)
        return f"{n} signature{'s' * (n != 1)}: " + ", ".join(parts)

    def _signature_clicked(self, item: QListWidgetItem) -> None:
        f = self.info.field(item.data(Qt.UserRole)) if self.info else None
        if f is not None and f.page is not None:
            self.view.go_to(f.page)

    def show_signature_details(self, item: QListWidgetItem) -> None:
        r = next((r for r in self.reports if r.field == item.data(Qt.UserRole)), None)
        if r is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle(f"Signature '{printable(r.field)}'")
        box.setIcon(QMessageBox.Information if r.ok else QMessageBox.Warning)
        lines = [f"<b>{r.status.upper()}</b>", f"Signer: {r.signer}", f"Issuer: {r.issuer}",
                 f"Covers: {r.coverage}", *(f"Problem: {p}" for p in r.problems)]
        lines = [lines[0], *(html.escape(printable(line)) for line in lines[1:])]
        box.setText("<br>".join(lines))
        box.setDetailedText(printable(r.details, keep_newlines=True))
        box.exec()

    def _fill_fields(self) -> None:
        self.fields_list.clear()
        for f in self.info.fields if self.info else []:
            state = "signed" if f.filled else ("LOCKED" if f.locked else "empty")
            where = f"p. {f.page + 1}" if f.page is not None else "invisible"
            item = QListWidgetItem(f"{printable(f.name)}\n   {state}, {where}")
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
            target = f"Prepared field '{self.view.selected_field}'"
        elif (pb := self.view.placement_box()) is not None:
            page, box = pb
            target = (f"New signature on page {page + 1}\n"
                      f"at {box[0]:.0f}, {box[1]:.0f} pt, "
                      f"{box[2] - box[0]:.0f} × {box[3] - box[1]:.0f} pt")
        for i in range(self.fields_list.count()):
            item = self.fields_list.item(i)
            item.setSelected(item.data(Qt.UserRole) == self.view.selected_field)
        self.target_label.setText(target or "Click on the page to place a signature, "
                                  "or pick an empty field.")
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
            QMessageBox.critical(self, "Cannot sign", str(e))
            return
        if reason := self.info.blocker(field_name, new_visible=box is not None):
            if QMessageBox.warning(
                self, "Existing signature will break",
                f"Signing will invalidate an existing signature:\n{reason}\n\nSign anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return

        default = self.path.with_name(
            self.path.name if self.path.stem.endswith("-signed")
            else f"{self.path.stem}-signed.pdf")
        out, _ = QFileDialog.getSaveFileName(self, "Save signed PDF", str(default), "PDF (*.pdf)")
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
            QMessageBox.critical(self, "Signing failed", str(e))
            return
        finally:
            QGuiApplication.restoreOverrideCursor()

        self.open(out, self.pdf_password, page=self.view.page)
        self.statusBar().showMessage(
            f"Signed '{name}' → {out.name}. You can add further signatures.", 15000)

    def _ask_unbusy(self, prompt: str) -> str | None:
        QGuiApplication.restoreOverrideCursor()
        try:
            return ask_secret(self, prompt)
        finally:
            QGuiApplication.setOverrideCursor(Qt.WaitCursor)


ICON = Path(__file__).with_name("icon.png")


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
        QMessageBox.critical(None, "PDF Signer", f"Cannot read the configuration:\n{e}")
        return 1
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
