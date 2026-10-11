"""Records where the GUI hangs: when the GUI thread stops processing events
for STALL_SECONDS (desktops report "not responding" after about 5 s), the
stacks of all threads are written to a log file.

faulthandler does the writing from its own C thread, so it works even when
the GUI thread is stuck in native code that holds the GIL. The GUI thread
re-arms it twice a second; a freeze is a missed re-arm. Once the GUI thread
runs again, a line with the time and the freeze's length is added.
"""

from __future__ import annotations

import faulthandler
import time
from datetime import datetime
from pathlib import Path

from platformdirs import user_log_dir
from PySide6.QtCore import QObject, QTimer

from .. import environ

STALL_SECONDS = 4
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
        faulthandler.dump_traceback_later(STALL_SECONDS, file=self.file)

    def stop(self) -> None:
        self.timer.stop()
        faulthandler.cancel_dump_traceback_later()
        self.file.close()
