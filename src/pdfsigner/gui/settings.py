"""Program settings (the config file's top-level keys): language and trust roots.

Certificate profiles have their own dialog (certdialog.CertManagerDialog).
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
    QVBoxLayout,
)

from .. import i18n
from ..config import Config, default_config_path
from ..i18n import _
from ..signing import SigningError
from ..validation import load_trust_roots
from .screen import ElidedLabel, fit_to_screen


class SettingsDialog(QDialog):
    """Edits a copy of the config; ``config`` holds the result after OK."""

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Settings"))
        fit_to_screen(self, 620, 420)
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
                        "are shown as valid; Mozilla's root list is always trusted. Use this "
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

        lay = QVBoxLayout(self)
        lay.addLayout(general)
        lay.addWidget(trust_box, 1)
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
        self.language_changed = language != self.config.language
        self.config.language = language
        self.config.trust = self.trust_paths()
        try:
            self.config.save()
        except OSError as e:
            QMessageBox.critical(self, _("Settings"),
                                 _("Cannot save the configuration:\n{error}").format(error=e))
            return
        self.accept()
