"""`clara-server --headless`: keep running when the terminal that started it goes away.

An SSH session that ends sends SIGHUP to what it started, and takes the terminal with it: the server dies, or
fails on its first write to a terminal that is gone. Headless mode ignores SIGHUP, gives up the terminal's
standard streams and writes the log to `data/logs/clara-server.log` (rotated) instead. There is no console;
`kill <pid>` (SIGTERM) still stops the server the careful way, and so does `clara-admin /stop`.

Windows has no SIGHUP: there it only drops the console and logs to the file.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .settings import Settings

LOG_NAME = "clara-server.log"
LOG_MAX_BYTES = 5_000_000
LOG_BACKUPS = 5


def log_path(settings: Settings) -> Path:
    return settings.logs_dir / LOG_NAME


def file_handler(path: Path) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    return RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")


def detach_from_terminal() -> None:
    """Ignore the hangup, and point stdin, stdout and stderr at nothing. Call it once the log is going to a
    file: what is printed after this is lost."""
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError, AttributeError):
            pass
    null = os.open(os.devnull, os.O_RDWR)
    try:
        for descriptor in (0, 1, 2):
            os.dup2(null, descriptor)
    finally:
        os.close(null)
