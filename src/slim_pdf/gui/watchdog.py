"""Records where the GUI hangs: when the GUI thread stops processing events
for STALL_SECONDS (desktops report "not responding" after about 5 s), the
stacks of all threads are written to a log file.

faulthandler does the writing from its own C thread, so it works even when
the GUI thread is stuck in native code that holds the GIL. The GUI thread
re-arms it twice a second; a freeze is a missed re-arm. Once the GUI thread
runs again, a line with the time and the freeze's length is added; shorter
stalls (from SLOW_SECONDS) get such a line, too, without stacks.

Every start adds a line (version, process, Qt platform, session type), so a
log without freezes still shows that it was written by the running program.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from platformdirs import user_log_dir
from PySide6.QtCore import QObject, QTimer

from .. import environ

STALL_SECONDS = 4
SLOW_SECONDS = 1.0
BEAT_MS = 500
MAX_LOG_BYTES = 1_000_000  # start afresh beyond this


def log_path() -> Path:
    base = environ("LOG_DIR") or user_log_dir("slim-pdf", appauthor=False)
    return Path(base) / "freezes.log"


class Watchdog(QObject):
    def __init__(self, parent: QObject | None = None, path: Path | None = None):
        super().__init__(parent)
        self.path = path or log_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        big = self.path.exists() and self.path.stat().st_size > MAX_LOG_BYTES
        # stays open: faulthandler writes to its descriptor, without Python
        self.file = open(self.path, "w" if big else "a", encoding="utf-8")  # noqa: SIM115
        self.last = time.monotonic()
        self.file.write(f"=== {datetime.now():%Y-%m-%d %H:%M:%S}: started {self.who()}\n")
        self.file.flush()
        self.timer = QTimer(self)
        self.timer.setInterval(BEAT_MS)
        self.timer.timeout.connect(self.beat)
        self.timer.start()
        self.beat()

    def beat(self) -> None:
        now = time.monotonic()
        gap = now - self.last
        self.last = now
        if gap >= STALL_SECONDS:  # the stacks above were written meanwhile
            self.file.write(f"=== {datetime.now():%Y-%m-%d %H:%M:%S}: the GUI did not respond "
                            f"for {gap:.1f} s (stacks above, taken after "
                            f"{STALL_SECONDS} s)\n\n")
            self.file.flush()
        elif gap >= SLOW_SECONDS + BEAT_MS / 1000:
            self.file.write(f"=== {datetime.now():%Y-%m-%d %H:%M:%S}: the GUI was busy for "
                            f"{gap - BEAT_MS / 1000:.1f} s\n")
            self.file.flush()
        faulthandler.dump_traceback_later(STALL_SECONDS, file=self.file)

    @staticmethod
    def who() -> str:
        from importlib import metadata

        from PySide6.QtGui import QGuiApplication

        try:
            version = metadata.version("slim-pdf")
        except metadata.PackageNotFoundError:
            version = "?"
        platform = QGuiApplication.platformName() if QGuiApplication.instance() else "?"
        session = os.environ.get("XDG_SESSION_TYPE", "")
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
        return (f"slim-pdf {version}, pid {os.getpid()}, Python {sys.version.split()[0]}, "
                f"Qt platform {platform}"
                + (f", session {session}" if session else "")
                + (f", desktop {desktop}" if desktop else ""))

    def stop(self) -> None:
        self.timer.stop()
        faulthandler.cancel_dump_traceback_later()
        self.file.close()
