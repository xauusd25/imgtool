"""
Generic large-file split/merge, used for super.img <-> super.img.000, .001, ...

Splitting is plain byte-range splitting (not sparse-chunk splitting), so it
works identically for any file and always reconstructs byte-for-byte.
"""

import os
import re

_READ_BUF = 1024 * 1024
_PART_RE = re.compile(r"^(.*)\.(\d{3,})$")


def split_file(src, out_dir, chunk_size_mb=1024):
    """Split `src` into <out_dir>/<basename>.000, .001, ... of chunk_size_mb MB each."""
    chunk_size = int(chunk_size_mb) * 1024 * 1024
    if chunk_size <= 0:
        raise ValueError("chunk_size_mb must be positive")
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.basename(src)
    idx = 0
    written_parts = []
    with open(src, "rb") as f:
        while True:
            remaining = chunk_size
            part_path = os.path.join(out_dir, f"{base}.{idx:03d}")
            any_data = False
            with open(part_path, "wb") as out:
                while remaining > 0:
                    buf = f.read(min(_READ_BUF, remaining))
                    if not buf:
                        break
                    out.write(buf)
                    remaining -= len(buf)
                    any_data = True
            if not any_data:
                os.remove(part_path)
                break
            written_parts.append(part_path)
            idx += 1
    return written_parts


def _sort_key(path):
    m = _PART_RE.match(os.path.basename(path))
    if m:
        return (m.group(1), int(m.group(2)))
    return (os.path.basename(path), -1)


def find_parts(any_part_path):
    """Given one part file (or a directory), return all sibling parts in order."""
    if os.path.isdir(any_part_path):
        folder = any_part_path
        names = os.listdir(folder)
    else:
        folder = os.path.dirname(os.path.abspath(any_part_path)) or "."
        names = os.listdir(folder)

    m = _PART_RE.match(os.path.basename(any_part_path)) if not os.path.isdir(any_part_path) else None
    base = m.group(1) if m else None

    candidates = []
    for n in names:
        mm = _PART_RE.match(n)
        if mm and (base is None or mm.group(1) == base):
            candidates.append(os.path.join(folder, n))

    candidates.sort(key=_sort_key)
    return candidates


def merge_files(part_paths, dst):
    """Concatenate part_paths (in the given order) into dst."""
    if not part_paths:
        raise ValueError("No part files given to merge")
    with open(dst, "wb") as out:
        for p in part_paths:
            with open(p, "rb") as f:
                while True:
                    buf = f.read(_READ_BUF)
                    if not buf:
                        break
                    out.write(buf)
    return dst
