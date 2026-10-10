"""Certificate manager dialog: edits the same config file as ``pdfsigner certs``."""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import secretstore, systemstore
from ..config import (
    CERT_TYPES,
    KEEPASS_SOURCES,
    PKCS11_EXAMPLE,
    SECRET_SOURCES,
    CertProfile,
    Config,
)
from ..signing import SigningError, describe_signer, open_signer
from .dialogs import ask_secret
from .document import signature_pixmap

SECRET_LABELS = {
    "prompt": "Ask every time",
    "keyring": "OS keyring",
    "keepass": "KeePass database (.kdbx)",
    "keepassxc": "KeePassXC (keepassxc-cli, YubiKey)",
    "none": "No passphrase",
}


def _path_row(edit: QLineEdit, caption: str, filt: str) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(edit)
    btn = QPushButton("…")
    btn.setFixedWidth(32)

    def browse():
        path, _ = QFileDialog.getOpenFileName(w, caption, edit.text(), filt)
        if path:
            edit.setText(path)

    btn.clicked.connect(browse)
    lay.addWidget(btn)
    return w


class CertManagerDialog(QDialog):
    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Certificates")
        self.resize(820, 560)
        self.config = copy.deepcopy(config)
        self._current: str | None = None
        self._loading = False

        # left: list + buttons
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_select)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add)
        del_btn = QPushButton("Remove")
        del_btn.clicked.connect(self._remove)
        default_btn = QPushButton("Make default")
        default_btn.clicked.connect(self._make_default)
        left = QVBoxLayout()
        left.addWidget(self.list)
        row = QHBoxLayout()
        for b in (add_btn, del_btn):
            row.addWidget(b)
        left.addLayout(row)
        left.addWidget(default_btn)

        # right: form
        self.e = {}  # attribute -> widget

        def line(attr, placeholder=""):
            w = QLineEdit()
            w.setPlaceholderText(placeholder)
            self.e[attr] = w
            return w

        self.type_combo = QComboBox()
        self.type_combo.addItems(CERT_TYPES)
        self.type_combo.currentTextChanged.connect(self._update_visibility)

        general = QFormLayout()
        general.addRow("Type", self.type_combo)

        self.p12_box = QGroupBox("PKCS#12 file")
        f = QFormLayout(self.p12_box)
        f.addRow("File", _path_row(line("path", "~/certs/me.p12 (empty if stored in KeePass)"),
                                   "PKCS#12 file", "PKCS#12 (*.p12 *.pfx);;All files (*)"))

        self.sys_box = QGroupBox("System certificate store (Windows / macOS)")
        f = QFormLayout(self.sys_box)
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(line("system_cert", "thumbprint or part of the subject"))
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose_system_cert)
        lay.addWidget(choose)
        f.addRow("Certificate", row)

        self.p11_box = QGroupBox("PKCS#11 token / smartcard")
        f = QFormLayout(self.p11_box)
        f.addRow("Module", _path_row(line("pkcs11_lib", PKCS11_EXAMPLE),
                                     "PKCS#11 module", "PKCS#11 modules (*.so *.so.* *.dylib *.dll);;All files (*)"))
        f.addRow("Token label", line("token_label"))
        f.addRow("Key label", line("key_label"))
        f.addRow("Cert label", line("cert_label", "defaults to key label"))

        appearance = QGroupBox("Appearance")
        f = QFormLayout(appearance)
        img_edit = line("image", "PNG with transparent background, or PDF (vector)")
        img_edit.textChanged.connect(self._update_preview)
        f.addRow("Signature image", _path_row(img_edit, "Signature image",
                                              "Images (*.png *.jpg *.jpeg *.pdf);;All files (*)"))
        self.preview = QLabel()
        self.preview.setFixedHeight(60)
        self.preview.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        f.addRow("", self.preview)
        self.stamp_text = QPlainTextEdit()
        self.stamp_text.setFixedHeight(48)
        self.stamp_text.setPlaceholderText("empty = image only; %(signer)s, %(ts)s are replaced")
        f.addRow("Stamp text", self.stamp_text)

        meta = QGroupBox("Signature details")
        f = QFormLayout(meta)
        f.addRow("Reason", line("reason"))
        f.addRow("Location", line("location"))
        f.addRow("Contact", line("contact_info"))
        f.addRow("Timestamp URL", line("timestamp_url", "optional RFC 3161 TSA"))
        self.ltv_check = QCheckBox("Embed validation data (LTV)")
        self.ltv_check.setToolTip(
            "Embed the certificate chain and revocation info so the signature can still "
            "be verified after the certificate expires (PAdES B-LT; B-LTA with a "
            "timestamp server). Needs network access and a trusted chain while signing.")
        f.addRow("", self.ltv_check)

        secret = QGroupBox("Passphrase / PIN")
        f = QFormLayout(secret)
        self.secret_combo = QComboBox()
        for s in SECRET_SOURCES:
            self.secret_combo.addItem(SECRET_LABELS[s], s)
        self.secret_combo.currentIndexChanged.connect(self._update_visibility)
        f.addRow("Source", self.secret_combo)
        self.keyring_btn = QPushButton("Store passphrase in keyring…")
        self.keyring_btn.clicked.connect(self._store_keyring)
        f.addRow("", self.keyring_btn)
        self.kp_widgets = [
            ("KeePass file", _path_row(line("keepass_db"), "KeePass database",
                                       "KeePass (*.kdbx);;All files (*)")),
            ("Key file", _path_row(line("keepass_keyfile", "optional"), "Key file", "All files (*)")),
            ("Entry", line("keepass_entry", "Group/Title")),
            ("Attachment", line("keepass_attachment", "optional: .p12 stored on the entry")),
        ]
        self.yubikey_row = line("keepass_yubikey", "optional: slot[:serial], e.g. 2")
        self.kp_widgets.append(("YubiKey", self.yubikey_row))
        for label, w in self.kp_widgets:
            f.addRow(label, w)
        self._secret_form = f

        test_btn = QPushButton("Test: unlock and show certificate")
        test_btn.clicked.connect(self._test)

        right = QVBoxLayout()
        right.addLayout(general)
        self.secret_box = secret
        for box in (self.p12_box, self.p11_box, self.sys_box, appearance, meta, secret):
            right.addWidget(box)
        right.addWidget(test_btn)
        right.addStretch()
        self.form = QWidget()
        self.form.setLayout(right)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        main = QHBoxLayout()
        main.addLayout(left, 1)
        main.addWidget(self.form, 3)
        outer = QVBoxLayout(self)
        outer.addLayout(main)
        hint = QLabel(f"Config file: {self.config.path}")
        hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        hint.setStyleSheet("color: gray")
        outer.addWidget(hint)
        outer.addWidget(buttons)

        self._refresh_list()

    # list ---------------------------------------------------------------------

    def _refresh_list(self, select: str | None = None) -> None:
        self._loading = True
        self.list.clear()
        for name in self.config.certs:
            label = f"{name}  (default)" if name == self.config.default else name
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, name)
            self.list.addItem(item)
        self._loading = False
        select = select or self._current or self.config.default
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == select:
                self.list.setCurrentRow(i)
                break
        else:
            if self.list.count():
                self.list.setCurrentRow(0)
            else:
                self._current = None
        self.form.setEnabled(self.list.count() > 0)

    def _on_select(self, cur, prev) -> None:
        if self._loading:
            return
        if prev is not None and self._current:
            self._store_form()
        self._current = cur.data(Qt.UserRole) if cur else None
        if self._current:
            self._load_form(self.config.certs[self._current])

    def _add(self) -> None:
        name, ok = QInputDialog.getText(self, "New certificate", "Name:")
        name = name.strip()
        if not ok or not name:
            return
        if name in self.config.certs:
            QMessageBox.warning(self, "Certificates", f"{name!r} already exists")
            return
        if self._current:
            self._store_form()
        self.config.certs[name] = CertProfile(name=name)
        if not self.config.default:
            self.config.default = name
        self._current = name
        self._refresh_list(name)

    def _remove(self) -> None:
        if not self._current:
            return
        if QMessageBox.question(self, "Certificates", f"Remove {self._current!r}?") \
                != QMessageBox.Yes:
            return
        self.config.remove(self._current)
        self._current = None
        self._refresh_list()

    def _make_default(self) -> None:
        if self._current:
            self._store_form()
            self.config.default = self._current
            self._refresh_list(self._current)

    # form ---------------------------------------------------------------------

    def _load_form(self, p: CertProfile) -> None:
        self.type_combo.setCurrentText(p.type)
        for attr, w in self.e.items():
            w.setText(getattr(p, attr))
        self.stamp_text.setPlainText(p.stamp_text)
        self.ltv_check.setChecked(p.ltv)
        self.secret_combo.setCurrentIndex(SECRET_SOURCES.index(p.secret))
        self._update_visibility()
        self._update_preview()

    def _store_form(self) -> CertProfile:
        p = self.config.certs[self._current]
        p.type = self.type_combo.currentText()
        for attr, w in self.e.items():
            setattr(p, attr, w.text().strip())
        p.stamp_text = self.stamp_text.toPlainText()
        p.ltv = self.ltv_check.isChecked()
        p.secret = self.secret_combo.currentData()
        return p

    def _update_visibility(self) -> None:
        is_p12 = self.type_combo.currentText() == "pkcs12"
        self.p12_box.setVisible(is_p12)
        self.p11_box.setVisible(self.type_combo.currentText() == "pkcs11")
        self.sys_box.setVisible(self.type_combo.currentText() == "system")
        # Windows/macOS ask for PINs themselves
        self.secret_box.setVisible(self.type_combo.currentText() != "system")
        source = self.secret_combo.currentData()
        self.keyring_btn.setVisible(source == "keyring")
        for _, w in self.kp_widgets:
            self._secret_form.setRowVisible(w, source in KEEPASS_SOURCES)
        self._secret_form.setRowVisible(self.yubikey_row, source == "keepassxc")

    def _update_preview(self) -> None:
        path = self.e["image"].text().strip()
        pm = signature_pixmap(CertProfile(name="_", image=path).image_path) if path else QPixmap()
        if pm.isNull():
            self.preview.setText("(no image)" if not path else "(cannot load image)")
        else:
            self.preview.setPixmap(pm.scaledToHeight(56, Qt.SmoothTransformation))

    # actions ------------------------------------------------------------------

    def _store_keyring(self) -> None:
        if not self._current:
            return
        secret = ask_secret(self, f"Passphrase/PIN for '{self._current}'")
        if secret is None:
            return
        try:
            secretstore.keyring_set(self._current, secret)
        except Exception as e:  # keyring backends raise various errors
            QMessageBox.critical(self, "Keyring", f"Could not store secret:\n{e}")
            return
        QMessageBox.information(
            self, "Keyring", f"Stored in {secretstore.keyring_backend_name()}")

    def _choose_system_cert(self) -> None:
        try:
            backend = systemstore.get_backend()
            try:
                identities = backend.identities()
            finally:
                backend.close()
        except SigningError as e:
            QMessageBox.critical(self, "System certificate store", str(e))
            return
        if not identities:
            QMessageBox.information(self, "System certificate store",
                                    f"No certificates with a private key in the {backend.name}.")
            return
        items = [i.describe() for i in identities]
        choice, ok = QInputDialog.getItem(self, "System certificate store",
                                          "Certificate:", items, 0, False)
        if ok:
            self.e["system_cert"].setText(identities[items.index(choice)].thumbprint)

    def _test(self) -> None:
        if not self._current:
            return
        p = self._store_form()
        try:
            p.validate()
            creds = secretstore.resolve_credentials(p, lambda prompt: ask_secret(self, prompt))
            with open_signer(p, creds) as signer:
                info = describe_signer(signer)
        except secretstore.Cancelled:
            return
        except (ValueError, SigningError, secretstore.SecretError) as e:
            QMessageBox.critical(self, "Test failed", str(e))
            return
        QMessageBox.information(self, "Certificate OK", info)

    def _save(self) -> None:
        if self._current:
            self._store_form()
        for p in self.config.certs.values():
            try:
                p.validate()
            except ValueError as e:
                QMessageBox.warning(self, "Invalid certificate", str(e))
                self._refresh_list(p.name)
                return
        self.config.save()
        self.accept()
