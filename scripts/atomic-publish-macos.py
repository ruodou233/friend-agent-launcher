#!/usr/bin/env python3
"""Publish one path with macOS's atomic exclusive rename primitive."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys


# These flags are declared by the macOS SDK in sys/stdio.h.
RENAME_EXCL = 0x00000004
RENAME_NOFOLLOW_ANY = 0x10
USAGE_EXIT = 64
MACOS_SYSTEM_ALIASES = {
    b"/var": b"/private/var",
    b"/tmp": b"/private/tmp",
}


def blocked(message: str, exit_code: int = 1) -> int:
    """Emit a fixed message without exposing either caller-supplied path."""

    print(f"atomic publish: BLOCKED: {message}", file=sys.stderr)
    return exit_code


def canonical_path_without_untrusted_symlinks(path: bytes) -> bytes | None:
    """Canonicalize macOS system aliases while rejecting caller symlinks."""

    if any(component == b".." for component in path.split(b"/")):
        return None
    absolute = os.path.abspath(path)
    components = [component for component in absolute.split(b"/") if component]
    current = b"/"
    for index, component in enumerate(components):
        candidate = os.path.join(current, component)
        try:
            candidate_info = os.lstat(candidate)
        except FileNotFoundError:
            if index != len(components) - 1:
                return None
            return candidate
        except (OSError, ValueError):
            return None
        if stat.S_ISLNK(candidate_info.st_mode):
            allowed_target = MACOS_SYSTEM_ALIASES.get(candidate)
            if allowed_target is None or os.path.realpath(candidate) != allowed_target:
                return None
            current = allowed_target
            continue
        if index != len(components) - 1 and not stat.S_ISDIR(candidate_info.st_mode):
            return None
        current = candidate
    return current


def open_parent_directory(path: bytes) -> tuple[int, bytes]:
    """Open every parent component without following a directory symlink."""

    parent = os.path.dirname(path) or b"/"
    basename = os.path.basename(path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(b"/", directory_flags)
    try:
        for component in parent.split(b"/"):
            if component:
                next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
        return directory_fd, basename
    except Exception:
        os.close(directory_fd)
        raise


def source_is_publishable(parent_fd: int, basename: bytes) -> bool:
    try:
        source_info = os.stat(basename, dir_fd=parent_fd, follow_symlinks=False)
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(source_info.st_mode) or stat.S_ISDIR(source_info.st_mode)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return blocked("expected source and destination paths", USAGE_EXIT)
    if sys.platform != "darwin":
        return blocked("macOS native rename is unavailable")

    try:
        source = os.fsencode(argv[1])
        destination = os.fsencode(argv[2])
    except (TypeError, ValueError, UnicodeError):
        return blocked("source boundary is invalid")

    if b"\0" in source or b"\0" in destination:
        return blocked("source or destination boundary is invalid")
    canonical_source = canonical_path_without_untrusted_symlinks(source)
    canonical_destination = canonical_path_without_untrusted_symlinks(destination)
    if canonical_source is None or canonical_destination is None:
        return blocked("source or destination boundary is invalid")

    source_directory_fd = None
    destination_directory_fd = None
    try:
        source_directory_fd, source_basename = open_parent_directory(canonical_source)
        destination_directory_fd, destination_basename = open_parent_directory(canonical_destination)
        if not source_is_publishable(source_directory_fd, source_basename):
            return blocked("source boundary is invalid")
        libc = ctypes.CDLL(None, use_errno=True)
        renameatx_np = libc.renameatx_np
        renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx_np.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = renameatx_np(
            source_directory_fd,
            source_basename,
            destination_directory_fd,
            destination_basename,
            RENAME_EXCL | RENAME_NOFOLLOW_ANY,
        )
        error_number = ctypes.get_errno()
    except Exception:
        return blocked("macOS native rename is unavailable")
    finally:
        if source_directory_fd is not None:
            os.close(source_directory_fd)
        if destination_directory_fd is not None:
            os.close(destination_directory_fd)

    if result == 0:
        return 0
    if error_number in {errno.EEXIST, errno.EISDIR, errno.ENOTEMPTY}:
        return blocked("target already exists")
    return blocked("atomic no-clobber rename failed")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
