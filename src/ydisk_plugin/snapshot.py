"""Short-lived source bytes for conflict-safe edits of recently read Office files."""

import hashlib
import time

from .account import private_directory
from .disk import path_of

WINDOW = 86400
TOTAL = 128 * 1024 * 1024


def location(account, path, digest):
    key = hashlib.sha256((path_of(path) + "\0" + digest).encode()).hexdigest()
    return private_directory(account.directory / "recent-versions") / key


def remember(account, path, digest, data):
    target = location(account, path, digest)
    target.write_bytes(data)
    now, size = time.time(), 0
    for item in sorted(
        target.parent.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True
    ):
        info = item.stat()
        if now - info.st_mtime > WINDOW or size + info.st_size > TOTAL:
            item.unlink()
        else:
            size += info.st_size


def recall(account, path, digest):
    target = location(account, path, digest)
    if target.is_file() and time.time() - target.stat().st_mtime <= WINDOW:
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() == digest:
            return data
    return None
