"""
imgtool — Android .img / super.img toolkit

Usage:
  python -m imgtool img-unpack   <sparse.img> <out_raw.img>
  python -m imgtool img-repack   <raw.img> <out_sparse.img> [block_size]
  python -m imgtool super-unpack <super.img> <out_dir>
  python -m imgtool super-repack <super.img> <partition_dir> <out_super.img>
  python -m imgtool super-list   <super.img>
  python -m imgtool super-split  <super.img> <out_dir> [chunk_size_mb=1024]
  python -m imgtool super-merge  <first_part_or_dir> <out_super.img>
"""

import os
import sys

from . import sparse, liblp, splitmerge


def _fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def cmd_img_unpack(args):
    if len(args) != 2:
        print("Usage: img-unpack <sparse.img> <out_raw.img>")
        return 1
    src, dst = args
    if not sparse.is_sparse(src):
        print(f"'{src}' is not a sparse image (no sparse magic found).")
        print("It may already be a raw/flat image — nothing to unpack.")
        return 1
    sparse.unsparse(src, dst)
    print(f"Unpacked sparse image -> raw image: {dst}")
    print(f"  Size: {_fmt_bytes(os.path.getsize(dst))}")
    return 0


def cmd_img_repack(args):
    if len(args) not in (2, 3):
        print("Usage: img-repack <raw.img> <out_sparse.img> [block_size=4096]")
        return 1
    src, dst = args[0], args[1]
    blk_sz = int(args[2]) if len(args) == 3 else 4096
    if sparse.is_sparse(src):
        print(f"'{src}' is already a sparse image — nothing to repack.")
        return 1
    n_chunks = sparse.resparse(src, dst, blk_sz)
    print(f"Repacked raw image -> sparse image: {dst}")
    print(f"  Chunks: {n_chunks}, block size: {blk_sz}")
    print(f"  Size: {_fmt_bytes(os.path.getsize(dst))}")
    return 0


def cmd_super_list(args):
    if len(args) != 1:
        print("Usage: super-list <super.img>")
        return 1
    (super_path,) = args
    for name, size, n_ext in liblp.list_partitions(super_path):
        print(f"  {name:<24} {_fmt_bytes(size):>10}   ({n_ext} extent(s))")
    return 0


def cmd_super_unpack(args):
    if len(args) != 2:
        print("Usage: super-unpack <super.img> <out_dir>")
        return 1
    super_path, out_dir = args
    src = super_path
    tmp = None
    if sparse.is_sparse(super_path):
        print("super.img is sparse — converting to raw first...")
        tmp = super_path + ".raw.tmp"
        sparse.unsparse(super_path, tmp)
        src = tmp
    try:
        results = liblp.unpack_super(src, out_dir)
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    print(f"Unpacked {len(results)} partition(s) to '{out_dir}':")
    for name, size in results:
        print(f"  {name:<24} {_fmt_bytes(size)}")
    return 0


def cmd_super_repack(args):
    if len(args) != 3:
        print("Usage: super-repack <super.img> <partition_dir> <out_super.img>")
        return 1
    super_path, partition_dir, out_path = args
    if sparse.is_sparse(super_path):
        print("Error: input super.img is sparse. Run img-unpack on it first,")
        print("       repack with super-repack, then img-repack the result if needed.")
        return 1
    report = liblp.repack_super(super_path, partition_dir, out_path)
    if not report:
        print("No matching <partition>.img files were found in the partition directory.")
        print("(Filenames must match the logical partition names, e.g. system.img)")
        return 1
    print(f"Wrote '{out_path}':")
    for name, status, detail in report:
        print(f"  [{status:<8}] {name:<24} {detail}")
    return 0


def cmd_super_split(args):
    if len(args) not in (2, 3):
        print("Usage: super-split <super.img> <out_dir> [chunk_size_mb=1024]")
        return 1
    src, out_dir = args[0], args[1]
    chunk_mb = int(args[2]) if len(args) == 3 else 1024
    parts = splitmerge.split_file(src, out_dir, chunk_mb)
    print(f"Split '{src}' into {len(parts)} part(s) of up to {chunk_mb}MB in '{out_dir}':")
    for p in parts:
        print(f"  {os.path.basename(p)}  ({_fmt_bytes(os.path.getsize(p))})")
    return 0


def cmd_super_merge(args):
    if len(args) != 2:
        print("Usage: super-merge <first_part_or_dir> <out_super.img>")
        return 1
    first_part, out_path = args
    parts = splitmerge.find_parts(first_part)
    if not parts:
        print(f"No numbered part files found matching '{first_part}'")
        print("(expected files like myimage.super.000, .001, ...)")
        return 1
    print("Merging parts in this order:")
    for p in parts:
        print(f"  {os.path.basename(p)}")
    splitmerge.merge_files(parts, out_path)
    print(f"Merged -> '{out_path}' ({_fmt_bytes(os.path.getsize(out_path))})")
    return 0


COMMANDS = {
    "img-unpack": cmd_img_unpack,
    "img-repack": cmd_img_repack,
    "super-unpack": cmd_super_unpack,
    "super-repack": cmd_super_repack,
    "super-list": cmd_super_list,
    "super-split": cmd_super_split,
    "super-merge": cmd_super_merge,
}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    handler = COMMANDS.get(cmd)
    if handler is None:
        print(f"Unknown command: {cmd}\n")
        print(__doc__)
        return 1
    try:
        return handler(rest)
    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
