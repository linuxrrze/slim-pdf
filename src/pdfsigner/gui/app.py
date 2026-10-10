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
    QMessageBox,
    QPushButton,
    QSpinBox,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import secretstore, validation
from ..config import Config, load_config
from ..signing import (
    SigningBlocked,
    SigningError,
    check_target,
    image_aspect,
    inspect_pdf,
    open_signer,
    sign_pdf,
)
from .certdialog import CertManagerDialog
from .dialogs import ask_secret
from .document import PdfDocument, signature_pixmap
from .pageview import PageView

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

        self.setWindowTitle("PDF Signer")
        self.resize(1100, 900)
        self.view = PageView()
        self.view.targetChanged.connect(self._update_target)
        self.view.pageChanged.connect(self._sync_page_spin)
        self.setCentralWidget(self.view)

        self._build_toolbar()
        self._build_sign_panel()
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
                             triggered=lambda: self.view.clear_target()))

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
        start = str(self.path.parent) if self.path else ""
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", start, "PDF (*.pdf)")
        if path:
            self.open(Path(path))

    def open(self, path: Path, password: str | None = None) -> bool:
        try:
            info = inspect_pdf(path, password)
        except SigningError as e:
            if "password" in str(e):
                pw = ask_secret(self, f"Password for {path.name}")
                return pw is not None and self.open(path, pw)
            QMessageBox.critical(self, "Open", str(e))
            return False
        except Exception as e:  # malformed PDFs raise all sorts of things
            QMessageBox.critical(self, "Open", f"Cannot open {path}:\n{e}")
            return False

        if self.doc is not None:
            self.doc.close()
        self.doc = PdfDocument(str(path), password)
        self.path, self.pdf_password, self.info = path, password, info
        self.view.set_document(self.doc, info.fields)
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(len(self.doc))
        self.page_spin.setValue(1)
        self.page_spin.blockSignals(False)
        self.page_count.setText(f" / {len(self.doc)} ")
        self._fill_fields()
        self.setWindowTitle(f"{path.name} – PDF Signer")
        message = self._verify() or HINT
        if info.certification is not None:
            message = f"Document is certified ({info.certification.name}). " + message
        self.statusBar().showMessage(message)
        return True

    def _verify(self) -> str:
        """Validate the signatures and fill the list; returns a short summary."""
        self.sigs_list.clear()
        self.reports = []
        try:
            self.reports = validation.verify_pdf(self.path, trust=self.config.trust,
                                                 password=self.pdf_password)
        except Exception as e:  # never block opening a document
            self.sigs_list.addItem(f"Validation failed: {e}")
            return ""
        for r in self.reports:
            when = r.timestamp or r.signed_at
            text = f"{r.field}: {r.status}\n   {r.signer.removeprefix('Common Name: ')}"
            if when:
                text += f", {when:%Y-%m-%d %H:%M}"
            item = QListWidgetItem(_dot(STATUS_COLORS[r.status]), text)
            item.setData(Qt.UserRole, r.field)
            item.setToolTip("\n".join([r.coverage, *r.problems]))
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
        box.setWindowTitle(f"Signature '{r.field}'")
        box.setIcon(QMessageBox.Information if r.ok else QMessageBox.Warning)
        lines = [f"<b>{r.status.upper()}</b>", f"Signer: {r.signer}", f"Issuer: {r.issuer}",
                 f"Covers: {r.coverage}", *(f"Problem: {p}" for p in r.problems)]
        lines = [lines[0], *map(html.escape, lines[1:])]
        box.setText("<br>".join(lines))
        box.setDetailedText(r.details)
        box.exec()

    def _fill_fields(self) -> None:
        self.fields_list.clear()
        for f in self.info.fields if self.info else []:
            state = "signed" if f.filled else ("LOCKED" if f.locked else "empty")
            where = f"p. {f.page + 1}" if f.page is not None else "invisible"
            item = QListWidgetItem(f"{f.name}\n   {state}, {where}")
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
        if reason := self.info.blocker(field_name):
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
                    self.path, out, signer, profile,
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

        current_page = self.view.page
        self.open(out, self.pdf_password)
        self.view.go_to(current_page)
        self.statusBar().showMessage(
            f"Signed '{name}' → {out.name}. You can add further signatures.", 15000)

    def _ask_unbusy(self, prompt: str) -> str | None:
        QGuiApplication.restoreOverrideCursor()
        try:
            return ask_secret(self, prompt)
        finally:
            QGuiApplication.setOverrideCursor(Qt.WaitCursor)


def main(argv: list[str] | None = None, config_path: Path | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    app = QApplication.instance() or QApplication([sys.argv[0], *argv])
    app.setApplicationName("pdfsigner")
    win = MainWindow(load_config(config_path))
    win.show()
    files = [a for a in argv if not a.startswith("-")]
    if files:
        win.open(Path(files[0]))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
