"""OS file locking prevents two bot processes from sharing a profile."""

import os
from pathlib import Path
from typing import BinaryIO

from src.core.exceptions import HumanRequired


class ProfileLock:
    def __init__(self, directory: Path):
        self.path = directory / ".apple-buy-bot.lock"
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise HumanRequired(
                "Browser profile is already in use; close its other process"
            ) from None
        self._file = handle

    def release(self) -> None:
        if self._file is not None:
            # Closing releases the OS lock even after abnormal application exit.
            self._file.close()
            self._file = None
