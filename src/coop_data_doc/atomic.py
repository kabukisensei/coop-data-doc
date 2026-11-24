"""Replace a file without truncating the previous bytes."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path | str, text: str) -> None:
    """Replace ``path`` with UTF-8 LF ``text``. Raise ``OSError`` on failure.

    The temporary file is created in the same directory as ``path``, and
    ``os.replace`` publishes it. On Windows that replace updates an existing
    file when both paths are on the same volume.

    The temporary file is flushed and ``fsync``ed before the replace. On
    POSIX the parent directory is ``fsync``ed afterwards, best effort. A
    directory ``fsync`` that fails does not raise and does not undo a
    replace that already succeeded. Windows has no equivalent directory
    ``fsync`` here.

    A locked destination raises ``OSError`` on that attempt. On any failure
    before the replace, the previous file is left as it was and the temporary
    file is removed. The replaced file keeps the destination's permission
    bits. A new file uses ``0o666`` masked by the process umask, the same as
    ``open``.
    """
    destination = Path(path)
    directory = destination.parent
    fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=directory)
    tmp_path = Path(tmp_name)
    handle = None
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        fd = -1
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.chmod(tmp_path, _replacement_mode(destination))
        os.replace(tmp_path, destination)
    except BaseException:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        elif fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    _fsync_directory(directory)


def _replacement_mode(destination: Path) -> int:
    try:
        return destination.stat().st_mode & 0o777
    except FileNotFoundError:
        mask = os.umask(0)
        os.umask(mask)
        return 0o666 & ~mask


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        dir_fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass
