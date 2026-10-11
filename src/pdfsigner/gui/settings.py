"""Program settings: language and trust roots (the config file's top-level
keys) on the General tab, the certificate profiles on the Certificates tab.
"""

from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import i18n
from ..config import Config, default_config_path
from ..i18n import _
from ..signing import SigningError
from ..validation import load_trust_roots
from .certdialog import CertManager
from .screen import ElidedLabel, fit_to_screen


class SettingsDialog(QDialog):
    """Edits a copy of the config; ``config`` holds the result after OK.

    The General tab's changes are saved by OK. The Certificates tab saves
    each change at once (as ``pdfsigner certs`` does), so ``certs.changed``
    means ``config`` holds new profiles even after Cancel."""

    GENERAL, CERTIFICATES = 0, 1

    def __init__(self, config: Config, parent=None, tab: int = GENERAL):
        super().__init__(parent)
        self.setWindowTitle(_("Settings"))
        fit_to_screen(self, 720, 460)
        self.config = copy.deepcopy(config)
        self.language_changed = False

        self.language_combo = QComboBox()
        self.language_combo.addItem(_("System default"), "")
        for code, name in i18n.LANGUAGES.items():
            self.language_combo.addItem(name, code)
        self.language_combo.setCurrentIndex(
            max(0, self.language_combo.findData(self.config.language)))
        general = QFormLayout()
        general.addRow(_("Language"), self.language_combo)

        trust_box = QGroupBox(_("Additional trusted certificates"))
        hint = QLabel(_("Signatures by these certificates, or by certificates they issued, "
                        "are shown as valid; Mozilla's root list and the system's CA store "
                        "are always trusted. Use this "
                        "for your organisation's CA or for colleagues' self-signed "
                        "certificates."))
        hint.setWordWrap(True)
        self.trust_list = QListWidget()
        add = QPushButton(_("Add…"))
        add.clicked.connect(self._add_trust)
        self.remove_btn = QPushButton(_("Remove"))
        self.remove_btn.clicked.connect(self._remove_trust)
        self.trust_list.currentRowChanged.connect(
            lambda row: self.remove_btn.setEnabled(row >= 0))
        buttons = QVBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(self.remove_btn)
        buttons.addStretch()
        row = QHBoxLayout()
        row.addWidget(self.trust_list, 1)
        row.addLayout(buttons)
        box_lay = QVBoxLayout(trust_box)
        box_lay.addWidget(hint)
        box_lay.addLayout(row)
        for path in self.config.trust:
            self._add_item(path)
        self.remove_btn.setEnabled(False)

        where = ElidedLabel(_("Configuration file: {path}").format(
            path=self.config.path or default_config_path()))
        where.setStyleSheet("color: gray")
        where.setTextInteractionFlags(Qt.TextSelectableByMouse)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)

        general_tab = QWidget()
        lay = QVBoxLayout(general_tab)
        lay.addLayout(general)
        lay.addWidget(trust_box, 1)
        # the same config object: profile changes are saved with the other
        # settings as they were (language and trust are taken over by OK)
        self.certs = CertManager(self.config)
        self.tabs = QTabWidget()
        self.tabs.addTab(general_tab, _("General"))
        self.tabs.addTab(self.certs, _("Certificates"))
        self.tabs.setCurrentIndex(tab)

        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs, 1)
        lay.addWidget(where)
        lay.addWidget(box)

    def _add_item(self, path: str) -> None:
        """List entry: the path, plus the certificate's subject if it loads."""
        try:
            [cert] = load_trust_roots([path])
            text = f"{path}\n   {cert.subject.human_friendly}"
        except SigningError as e:
            text = f"{path}\n   {e}"
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, path)
        self.trust_list.addItem(item)

    def _add_trust(self) -> None:
        files, _filter = QFileDialog.getOpenFileNames(
            self, _("Trusted certificates"), str(Path.home()),
            _("Certificates") + " (*.pem *.crt *.cer *.der);;" + _("All files") + " (*)")
        present = set(self.trust_paths())
        for f in files:
            if f in present:
                continue
            try:
                load_trust_roots([f])
            except SigningError as e:
                QMessageBox.warning(self, _("Trusted certificates"), str(e))
                continue
            self._add_item(f)
            present.add(f)

    def _remove_trust(self) -> None:
        row = self.trust_list.currentRow()
        if row >= 0:
            self.trust_list.takeItem(row)

    def trust_paths(self) -> list[str]:
        return [self.trust_list.item(i).data(Qt.UserRole) for i in range(self.trust_list.count())]

    def _save(self) -> None:
        language = self.language_combo.currentData()
        old = self.config.language, self.config.trust
        self.config.language = language
        self.config.trust = self.trust_paths()
        try:
            self.config.save()
        except OSError as e:
            self.config.language, self.config.trust = old  # as in the file
            QMessageBox.critical(self, _("Settings"),
                                 _("Cannot save the configuration:\n{error}").format(error=e))
            return
        self.language_changed = language != old[0]
        self.accept()
