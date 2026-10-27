from __future__ import annotations

import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TextIO

MAX_BYTES = 5 * 1024 * 1024
NOTICE_MAX = 200
_NEWLINES = re.compile(r"\r\n|\r|\n")


def _fold(msg: str) -> str:
    return _NEWLINES.sub(" | ", msg)


class Log:
    def __init__(self, path: Path, command: str, debug: bool, out: Optional[TextIO] = None) -> None:
        self.path = Path(path)
        self.command = command
        self.debug_on = debug
        self.out = out if out is not None else sys.stdout

    def _rotate(self) -> None:
        try:
            if self.path.stat().st_size > MAX_BYTES:
                os.replace(self.path, self.path.parent / "last-sync.log.1")
        except OSError:
            pass

    def _write(self, level: str, msg: str) -> None:
        try:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            line = f"{ts} pid={os.getpid()} {self.command} {level} {_fold(msg)}\n"
            self._rotate()
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line.encode("utf-8", "replace"))
            finally:
                os.close(fd)
        except OSError:
            pass

    def debug(self, msg: str) -> None:
        if self.debug_on:
            self._write("DEBUG", msg)

    def info(self, msg: str) -> None:
        self._write("INFO", msg)

    def warn(self, msg: str) -> None:
        self._write("WARN", msg)

    def error(self, msg: str) -> None:
        self._write("ERROR", msg)

    def exception(self, msg: str) -> None:
        self._write("ERROR", msg)
        for line in traceback.format_exc().splitlines():
            self._write("ERROR", line)

    def notice(self, msg: str) -> None:
        try:
            self.out.write(_fold(msg)[:NOTICE_MAX] + "\n")
            self.out.flush()
        except OSError:
            pass
