"""Certificate manager dialog: edits the same config file as ``pdfsigner certs``."""

from __future__ import annotations

import copy
from pathlib import Path

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
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import secretstore, systemstore, tokens
from ..config import (
    KEEPASS_SOURCES,
    PKCS11_EXAMPLE,
    SECRET_SOURCES,
    CertProfile,
    Config,
    new_profile,
    supported_cert_types,
)
from ..i18n import N_, _
from ..signing import SigningError, describe_signer, open_signer
from .dialogs import ask_secret
from .screen import ElidedLabel, fit_to_screen, scrollable
from .document import signature_pixmap

SECRET_LABELS = {
    "prompt": N_("Ask every time"),
    "keyring": N_("OS keyring"),
    "keepass": N_("KeePass database (.kdbx)"),
    "keepassxc": N_("KeePassXC (keepassxc-cli, YubiKey)"),
    "none": N_("No passphrase"),
}
# Shown in the type combo box; the code is its item data and goes into the config.
TYPE_LABELS = {
    "pkcs12": "pkcs12",
    "pkcs11": "pkcs11",
    "system": N_("system"),
}
# The type column of the certificate list.
TYPE_NAMES = {
    "pkcs12": N_("PKCS#12 file"),
    "pkcs11": N_("PKCS#11 token"),
    "system": N_("System certificate store"),
}


def _summary(p: CertProfile) -> str:
    """Which certificate a profile uses, in a few words, for the list."""
    if p.type == "system":
        return p.system_cert
    if p.type == "pkcs11":
        key = p.key_id and _("key ID {id}").format(id=p.key_id) or p.key_label
        parts = [p.token_label or Path(p.pkcs11_lib).name, key]
        return " – ".join(x for x in parts if x)
    if p.secret in KEEPASS_SOURCES and not p.path:
        return _("KeePass: {entry}").format(entry=p.keepass_entry)
    return p.path


def _path_row(edit: QLineEdit, caption: str, filt: str, field: QWidget | None = None) -> QWidget:
    """``field`` (default: ``edit`` itself) with a "…" button to browse for a file."""
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(field or edit)
    btn = QPushButton("…")
    btn.setFixedWidth(32)

    def browse():
        path, _filter = QFileDialog.getOpenFileName(w, caption, edit.text(), filt)
        if path:
            edit.setText(path)

    btn.clicked.connect(browse)
    lay.addWidget(btn)
    return w


class CertManagerDialog(QDialog):
    """The list of certificate profiles. Profiles are edited in
    :class:`CertEditDialog`; every change is saved to the config file at once."""

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Certificates"))
        fit_to_screen(self, 720, 400)
        self.config = copy.deepcopy(config)
        self.changed = False  # the config file was written

        self.list = QTreeWidget()
        self.list.setHeaderLabels([_("Name"), _("Type"), _("Certificate")])
        self.list.setRootIsDecorated(False)
        self.list.setAllColumnsShowFocus(True)
        self.list.header().setStretchLastSection(True)
        self.list.currentItemChanged.connect(self._update_buttons)
        self.list.itemActivated.connect(lambda item, column: self._edit())

        self.add_btn = QPushButton(_("Add…"))
        self.add_btn.clicked.connect(self._add)
        self.edit_btn = QPushButton(_("Edit…"))
        self.edit_btn.clicked.connect(self._edit)
        self.remove_btn = QPushButton(_("Remove"))
        self.remove_btn.clicked.connect(self._remove)
        self.default_btn = QPushButton(_("Make default"))
        self.default_btn.clicked.connect(self._make_default)
        side = QVBoxLayout()
        for b in (self.add_btn, self.edit_btn, self.remove_btn, self.default_btn):
            side.addWidget(b)
        side.addStretch()

        main = QHBoxLayout()
        main.addWidget(self.list, 1)
        main.addLayout(side)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        outer = QVBoxLayout(self)
        outer.addLayout(main)
        hint = ElidedLabel(_("Config file: {path}").format(path=self.config.path))
        hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        hint.setStyleSheet("color: gray")
        outer.addWidget(hint)
        outer.addWidget(buttons)

        self._refresh_list()

    def current(self) -> str | None:
        item = self.list.currentItem()
        return item.data(0, Qt.UserRole) if item else None

    def _refresh_list(self, select: str | None = None) -> None:
        select = select or self.current() or self.config.default
        self.list.clear()
        for name, p in self.config.certs.items():
            label = (_("{name}  (default)").format(name=name) if name == self.config.default
                     else name)
            kind = _(TYPE_NAMES[p.type]) if p.type in TYPE_NAMES else p.type
            item = QTreeWidgetItem([label, kind, _summary(p)])
            item.setData(0, Qt.UserRole, name)
            item.setToolTip(2, _summary(p))
            if name == self.config.default:
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
            self.list.addTopLevelItem(item)
            if name == select:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.topLevelItemCount():
            self.list.setCurrentItem(self.list.topLevelItem(0))
        for column in (0, 1):
            self.list.resizeColumnToContents(column)
        self._update_buttons()

    def _update_buttons(self, *args) -> None:
        name = self.current()
        for b in (self.edit_btn, self.remove_btn):
            b.setEnabled(name is not None)
        self.default_btn.setEnabled(name is not None and name != self.config.default)

    def _exec_editor(self, dlg: CertEditDialog) -> int:
        return dlg.exec()  # a hook for the tests

    def _open_editor(self, name: str | None) -> None:
        dlg = CertEditDialog(self.config, name, self)
        if self._exec_editor(dlg):
            self.changed = True
            self._refresh_list(dlg.name)

    def _add(self) -> None:
        self._open_editor(None)

    def _edit(self) -> None:
        if self.current():
            self._open_editor(self.current())

    def _write(self) -> bool:
        try:
            self.config.save()
        except OSError as e:
            QMessageBox.critical(self, _("Certificates"),
                                 _("Could not save the configuration:\n{error}").format(error=e))
            return False
        self.changed = True
        return True

    def _remove(self) -> None:
        name = self.current()
        if not name:
            return
        if QMessageBox.question(self, _("Certificates"),
                                _("Remove {name!r}?").format(name=name)) != QMessageBox.Yes:
            return
        self.config.remove(name)
        self._write()
        self._refresh_list()

    def _make_default(self) -> None:
        name = self.current()
        if name:
            self.config.default = name
            self._write()
            self._refresh_list(name)


class CertEditDialog(QDialog):
    """Edit one profile of ``config`` (``name``), or a new one (``name=None``).
    *Save* checks it, puts it into ``config`` and writes the config file."""

    def __init__(self, config: Config, name: str | None, parent=None):
        super().__init__(parent)
        self.config = config
        self.name = name
        self.setWindowTitle(_("Edit certificate {name!r}").format(name=name) if name
                            else _("New certificate"))
        fit_to_screen(self, 640, 760)

        self.name_edit = QLineEdit(name or "")
        self.name_edit.setReadOnly(name is not None)
        self.name_edit.setPlaceholderText(_("e.g. Work, YubiKey"))

        self.e = {}  # attribute -> widget

        def line(attr, placeholder=""):
            w = QLineEdit()
            w.setPlaceholderText(placeholder)
            self.e[attr] = w
            return w

        self.type_combo = QComboBox()
        for t in supported_cert_types():
            self.type_combo.addItem(_(TYPE_LABELS[t]), t)
        self.type_combo.currentIndexChanged.connect(self._update_visibility)

        general = QFormLayout()
        general.addRow(_("Name"), self.name_edit)
        general.addRow(_("Type"), self.type_combo)

        self.p12_box = QGroupBox(_("PKCS#12 file"))
        f = QFormLayout(self.p12_box)
        f.addRow(_("File"), _path_row(
            line("path", _("~/certs/me.p12 (empty if stored in KeePass)")),
            _("PKCS#12 file"), "PKCS#12 (*.p12 *.pfx);;" + _("All files") + " (*)"))

        self.sys_box = QGroupBox(_("System certificate store (Windows / macOS)"))
        f = QFormLayout(self.sys_box)
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(line("system_cert", _("thumbprint or part of the subject")))
        choose = QPushButton(_("Choose…"))
        choose.clicked.connect(self._choose_system_cert)
        lay.addWidget(choose)
        f.addRow(_("Certificate"), row)

        self.p11_box = QGroupBox(_("PKCS#11 token / smartcard"))
        f = QFormLayout(self.p11_box)
        # editable: the modules found on this system, or any path
        self.module_combo = QComboBox()
        self.module_combo.setEditable(True)
        self.module_combo.setInsertPolicy(QComboBox.NoInsert)
        self.module_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.module_combo.setMinimumContentsLength(20)  # long paths mustn't widen the dialog
        self.module_combo.addItems(tokens.find_modules())
        self.module_combo.setToolTip(_(
            "The PKCS#11 modules found on this system; or enter the path of another one. "
            "Empty: Show token contents… asks all modules found."))
        self.e["pkcs11_lib"] = self.module_combo.lineEdit()
        self.module_combo.setCurrentIndex(-1)
        self.e["pkcs11_lib"].setPlaceholderText(PKCS11_EXAMPLE)
        f.addRow(_("Module"), _path_row(
            self.e["pkcs11_lib"], _("PKCS#11 module"),
            _("PKCS#11 modules") + " (*.so *.so.* *.dylib *.dll);;" + _("All files") + " (*)",
            self.module_combo))
        f.addRow(_("Token label"), line("token_label"))
        f.addRow(_("Key label"), line("key_label"))
        f.addRow(_("Key ID"), line("key_id", _("hex, e.g. 02 (YubiKey PIV slot 9c)")))
        f.addRow(_("Cert label"),
                 line("cert_label", _("default: the certificate with the key's ID")))
        read_token = QPushButton(_("Show token contents…"))
        read_token.setToolTip(_("List the keys and certificates on the token and pick one"))
        read_token.clicked.connect(self._choose_token_key)
        f.addRow("", read_token)

        appearance = QGroupBox(_("Appearance"))
        f = QFormLayout(appearance)
        img_edit = line("image", _("PNG with transparent background, or PDF (vector)"))
        img_edit.textChanged.connect(self._update_preview)
        f.addRow(_("Signature image"), _path_row(
            img_edit, _("Signature image"),
            _("Images") + " (*.png *.jpg *.jpeg *.pdf);;" + _("All files") + " (*)"))
        self.preview = QLabel()
        self.preview.setFixedHeight(60)
        self.preview.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        f.addRow("", self.preview)
        self.stamp_text = QPlainTextEdit()
        self.stamp_text.setFixedHeight(48)
        self.stamp_text.setPlaceholderText(
            _("empty = image only; %(signer)s, %(ts)s are replaced"))
        f.addRow(_("Stamp text"), self.stamp_text)

        meta = QGroupBox(_("Signature details"))
        f = QFormLayout(meta)
        f.addRow(_("Reason"), line("reason"))
        f.addRow(_("Location"), line("location"))
        f.addRow(_("Contact"), line("contact_info"))
        f.addRow(_("Timestamp URL"), line("timestamp_url", _("optional RFC 3161 TSA")))
        self.ltv_check = QCheckBox(_("Embed validation data (LTV)"))
        self.ltv_check.setToolTip(_(
            "Embed the certificate chain and revocation info so the signature can still "
            "be verified after the certificate expires (PAdES B-LT; B-LTA with a "
            "timestamp server). Needs network access and a trusted chain while signing."))
        f.addRow("", self.ltv_check)

        secret = QGroupBox(_("Passphrase / PIN"))
        f = QFormLayout(secret)
        self.secret_combo = QComboBox()
        for s in SECRET_SOURCES:
            self.secret_combo.addItem(_(SECRET_LABELS[s]), s)
        self.secret_combo.currentIndexChanged.connect(self._update_visibility)
        f.addRow(_("Source"), self.secret_combo)
        self.keyring_btn = QPushButton(_("Store passphrase in keyring…"))
        self.keyring_btn.clicked.connect(self._store_keyring)
        f.addRow("", self.keyring_btn)
        self.kp_widgets = [
            (_("KeePass file"), _path_row(line("keepass_db"), _("KeePass database"),
                                          "KeePass (*.kdbx);;" + _("All files") + " (*)")),
            (_("Key file"), _path_row(line("keepass_keyfile", _("optional")), _("Key file"),
                                      _("All files") + " (*)")),
            (_("Entry"), line("keepass_entry", _("Group/Title"))),
            (_("Attachment"),
             line("keepass_attachment", _("optional: .p12 stored on the entry"))),
        ]
        self.yubikey_row = line("keepass_yubikey", _("optional: slot[:serial], e.g. 2"))
        self.kp_widgets.append(("YubiKey", self.yubikey_row))
        for label, w in self.kp_widgets:
            f.addRow(label, w)
        self._secret_form = f

        test_btn = QPushButton(_("Test: unlock and show certificate"))
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
        outer = QVBoxLayout(self)
        outer.addWidget(scrollable(self.form))  # the form is taller than small screens
        outer.addWidget(buttons)

        self._load_form(config.certs[name] if name else new_profile(""))
        if name is None:
            self.name_edit.setFocus()

    # form ---------------------------------------------------------------------

    def _load_form(self, p: CertProfile) -> None:
        if self.type_combo.findData(p.type) < 0:
            # e.g. a Windows "system" profile in a config shared with Linux:
            # show it as it is (saving keeps the type), but marked
            self.type_combo.addItem(_("{type} (not available on this system)").format(
                type=_(TYPE_LABELS[p.type])), p.type)
        self.type_combo.setCurrentIndex(max(0, self.type_combo.findData(p.type)))
        for attr, w in self.e.items():
            w.setText(getattr(p, attr))
        self.stamp_text.setPlainText(p.stamp_text)
        self.ltv_check.setChecked(p.ltv)
        self.secret_combo.setCurrentIndex(SECRET_SOURCES.index(p.secret))
        self._update_visibility()
        self._update_preview()

    def _profile_name(self) -> str:
        return self.name or self.name_edit.text().strip()

    def _store_form(self) -> CertProfile:
        """The profile as the form shows it (``config`` is not changed)."""
        p = copy.deepcopy(self.config.certs[self.name]) if self.name else new_profile("")
        p.name = self._profile_name()
        p.type = self.type_combo.currentData()
        for attr, w in self.e.items():
            setattr(p, attr, w.text().strip())
        p.stamp_text = self.stamp_text.toPlainText()
        p.ltv = self.ltv_check.isChecked()
        p.secret = self.secret_combo.currentData()
        return p

    def _update_visibility(self) -> None:
        cert_type = self.type_combo.currentData()
        self.p12_box.setVisible(cert_type == "pkcs12")
        self.p11_box.setVisible(cert_type == "pkcs11")
        self.sys_box.setVisible(cert_type == "system")
        # Windows/macOS ask for PINs themselves
        self.secret_box.setVisible(cert_type != "system")
        source = self.secret_combo.currentData()
        self.keyring_btn.setVisible(source == "keyring")
        for _label, w in self.kp_widgets:
            self._secret_form.setRowVisible(w, source in KEEPASS_SOURCES)
        self._secret_form.setRowVisible(self.yubikey_row, source == "keepassxc")

    def _update_preview(self) -> None:
        path = self.e["image"].text().strip()
        pm = signature_pixmap(CertProfile(name="_", image=path).image_path) if path else QPixmap()
        if pm.isNull():
            self.preview.setText(_("(no image)") if not path else _("(cannot load image)"))
        else:
            self.preview.setPixmap(pm.scaledToHeight(56, Qt.SmoothTransformation))

    # actions ------------------------------------------------------------------

    def _store_keyring(self) -> None:
        name = self._profile_name()
        if not name:
            QMessageBox.warning(self, _("Keyring"), _("Enter a name for the certificate first."))
            return
        secret = ask_secret(self, _("Passphrase/PIN for '{name}'").format(name=name))
        if secret is None:
            return
        try:
            secretstore.keyring_set(name, secret)
        except Exception as e:  # keyring backends raise various errors
            QMessageBox.critical(self, _("Keyring"),
                                 _("Could not store secret:\n{error}").format(error=e))
            return
        QMessageBox.information(
            self, _("Keyring"),
            _("Stored in {backend}").format(backend=secretstore.keyring_backend_name()))

    def _choose_system_cert(self) -> None:
        try:
            backend = systemstore.get_backend()
            try:
                identities = backend.identities()
            finally:
                backend.close()
        except SigningError as e:
            QMessageBox.critical(self, _("System certificate store"), str(e))
            return
        if not identities:
            QMessageBox.information(
                self, _("System certificate store"),
                _("No certificates with a private key in the {store}.").format(
                    store=backend.name))
            return
        items = [i.describe() for i in identities]
        choice, ok = QInputDialog.getItem(self, _("System certificate store"),
                                          _("Certificate:"), items, 0, False)
        if ok:
            self.e["system_cert"].setText(identities[items.index(choice)].thumbprint)

    def _choose_token_key(self) -> None:
        module = self.e["pkcs11_lib"].text().strip()
        modules = [module] if module else tokens.find_modules()
        if not modules:
            QMessageBox.information(self, _("PKCS#11 token"), _(
                "No PKCS#11 module found in the usual places. Install OpenSC or your card's "
                "software, or enter the module's path."))
            return
        found, errors = [], []
        for m in modules:
            try:
                found += tokens.list_tokens(m, ask_pin=lambda label: ask_secret(
                    self, _("PIN for token '{label}' (Cancel: certificates only)").format(
                        label=label)))
            except SigningError as e:
                errors.append(str(e))
        if errors and not found:
            QMessageBox.critical(self, _("PKCS#11 token"), "\n".join(errors))
            return
        choices = [(t, e) for t in found for e in t.entries if e.id]
        if not choices:
            QMessageBox.information(self, _("PKCS#11 token"),
                                    _("No keys or certificates with an ID found.") if found
                                    else _("No token found."))
            return
        several = len({t.module for t, _e in choices}) > 1
        items = [(f"{Path(t.module).name} – " if several else "") + f"{t.label}: {e.describe()}"
                 for t, e in choices]
        choice, ok = QInputDialog.getItem(self, _("PKCS#11 token"), _("Key / certificate:"),
                                          items, 0, False)
        if ok:
            t, e = choices[items.index(choice)]
            self.e["pkcs11_lib"].setText(t.module)
            self.e["token_label"].setText(t.label)
            self.e["key_id"].setText(e.id_hex)
            self.e["key_label"].clear()
            self.e["cert_label"].clear()

    def _test(self) -> None:
        p = self._store_form()
        try:
            p.validate()
            creds = secretstore.resolve_credentials(p, lambda prompt: ask_secret(self, prompt))
            with open_signer(p, creds) as signer:
                info = describe_signer(signer)
        except secretstore.Cancelled:
            return
        except (ValueError, SigningError, secretstore.SecretError) as e:
            QMessageBox.critical(self, _("Test failed"), str(e))
            return
        QMessageBox.information(self, _("Certificate OK"), info)

    def _save(self) -> None:
        p = self._store_form()
        if not p.name:
            QMessageBox.warning(self, _("Invalid certificate"), _("The certificate needs a name."))
            return
        if self.name is None and p.name in self.config.certs:
            QMessageBox.warning(self, _("Invalid certificate"),
                                _("{name!r} already exists").format(name=p.name))
            return
        try:
            p.validate()
        except ValueError as e:
            QMessageBox.warning(self, _("Invalid certificate"), str(e))
            return
        old = (copy.deepcopy(self.config.certs.get(p.name)), self.config.default)
        self.config.certs[p.name] = p
        if not self.config.default:
            self.config.default = p.name
        try:
            self.config.save()
        except OSError as e:
            self.config.default = old[1]  # keep config and file in step
            if old[0] is None:
                del self.config.certs[p.name]
            else:
                self.config.certs[p.name] = old[0]
            QMessageBox.critical(self, _("Certificates"),
                                 _("Could not save the configuration:\n{error}").format(error=e))
            return
        self.name = p.name
        self.accept()
