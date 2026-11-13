"""Path safety: names that are safe on every platform, no links or reparse points, never a credentials file."""

from pathlib import Path
import os
import re
import stat
import unicodedata

from .settings import credential_files

WINDOWS_INVALID = re.compile(r'[\x00-\x1f<>:"\\|?*\ud800-\udfff]')
WINDOWS_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9])")
WINDOWS_SHORT_ALIAS = re.compile(r"[^.]{1,6}~[0-9]+(?:\.[^.]{1,3})?")


def safe_relative(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\0" in name:
        raise ValueError("Unsafe artifact path.")
    if name.startswith("/") or any(p in ("", ".", "..") for p in name.split("/")):
        raise ValueError("Unsafe artifact path.")
    return name


def input_paths(names):
    """Validate the whole Linux input tree, including file/directory conflicts."""
    nodes = {}
    for name in names:
        parts = tuple(safe_relative(name).split("/"))
        for index in range(1, len(parts) + 1):
            key, is_file = parts[:index], index == len(parts)
            if key in nodes and (nodes[key] != is_file or is_file):
                raise ValueError("Input paths collide as files or directories.")
            nodes[key] = is_file


def portable_paths(names):
    """Return paths whose components can be represented distinctly on Windows."""
    paths, nodes = {}, {}
    for name in names:
        parts = tuple(safe_relative(name).split("/"))
        if name in paths:
            raise ValueError("Duplicate artifact path.")
        key = ()
        for index, part in enumerate(parts, 1):
            if WINDOWS_INVALID.search(part) or part.endswith((" ", ".")):
                raise ValueError("Unsafe Windows artifact path.")
            folded = unicodedata.normalize("NFKC", part).upper().casefold()
            stem = folded.split(".", 1)[0].rstrip(" ")
            if (
                WINDOWS_DEVICE.fullmatch(stem.upper())
                or WINDOWS_SHORT_ALIAS.fullmatch(folded)
                or len(part.encode("utf-16-le")) > 510
            ):
                raise ValueError("Reserved or unsupported Windows artifact path.")
            key += (folded,)
            node = (part, index == len(parts))
            if nodes.setdefault(key, node) != node:
                raise ValueError("Colliding artifact path.")
        paths[name] = parts
    return paths


def credential_name(name):
    return name.casefold().startswith(".env")


def is_link_or_reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def validate_input_source(path):
    """Check the original path and return its absolute, validated file path."""
    path = Path(path).absolute()
    if credential_name(path.name):
        raise ValueError("Never upload a .env file.")
    for parent in reversed(path.parents):
        if is_link_or_reparse(parent.lstat()):
            raise ValueError("Input source cannot traverse a symlink or reparse point.")
    info = path.lstat()
    if is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise ValueError("Input must be a regular file, not a symlink or reparse point.")
    resolved = path.resolve(strict=True)
    credential_infos = []
    for credential in credential_files():  # every file the lookup could use
        try:
            credential_infos.append(credential.stat())
        except FileNotFoundError:
            pass
    if credential_name(resolved.name) or any(
        os.path.samestat(info, credential_info) for credential_info in credential_infos
    ):
        raise ValueError("Never upload a .env file or an alias of it.")
    return resolved
