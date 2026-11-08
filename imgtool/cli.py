"""
imgtool — Android .img / super.img toolkit
by @XAU_USD_25

Usage:
  python -m imgtool img-unpack   <sparse.img> <out_raw.img>
  python -m imgtool img-repack   <raw.img> <out_sparse.img> [block_size]
  python -m imgtool super-unpack <super.img> <out_dir>
  python -m imgtool super-repack <super.img> <partition_dir> <out_super.img>
  python -m imgtool super-list   <super.img>
  python -m imgtool super-split  <super.img> <out_dir> [chunk_size_mb=1024]
  python -m imgtool super-merge  <first_part_or_dir> <out_super.img>
  python -m imgtool payload-list    <payload.bin>
  python -m imgtool payload-extract <payload.bin> <out_dir> [partition,...]
  python -m imgtool payload-create  <partition_dir> <out_payload.bin>
"""

import os
import sys

from . import sparse, liblp, splitmerge, payload


def _resolve_out_file(dst, src, suffix):
    """If dst is (or looks like) a directory, append a filename derived from src."""
    if os.path.isdir(dst) or dst.endswith(("/", "\\")):
        base = os.path.splitext(os.path.basename(src))[0]
        os.makedirs(dst, exist_ok=True)
        return os.path.join(dst, f"{base}{suffix}")
    parent = os.path.dirname(dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return dst


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
    dst = _resolve_out_file(dst, src, ".raw.img")
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
    dst = _resolve_out_file(dst, src, ".sparse.img")
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
    out_path = _resolve_out_file(out_path, super_path, ".repacked.img")
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
    if os.path.isdir(out_path) or out_path.endswith(("/", "\\")):
        merged_name = splitmerge._sort_key(parts[0])[0] or "merged.img"
        os.makedirs(out_path, exist_ok=True)
        out_path = os.path.join(out_path, merged_name)
    else:
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    print("Merging parts in this order:")
    for p in parts:
        print(f"  {os.path.basename(p)}")
    splitmerge.merge_files(parts, out_path)
    print(f"Merged -> '{out_path}' ({_fmt_bytes(os.path.getsize(out_path))})")
    return 0


def cmd_payload_list(args):
    if len(args) != 1:
        print("Usage: payload-list <payload.bin>")
        return 1
    (payload_path,) = args
    for name, size, n_ops, supported in payload.list_payload(payload_path):
        size_str = _fmt_bytes(size) if size is not None else "?"
        flag = "" if supported else "  [delta/unsupported ops]"
        print(f"  {name:<24} {size_str:>10}   ({n_ops} op(s)){flag}")
    return 0


def cmd_payload_extract(args):
    if len(args) not in (2, 3):
        print("Usage: payload-extract <payload.bin> <out_dir> [partition1,partition2,...]")
        return 1
    payload_path, out_dir = args[0], args[1]
    only = set(args[2].split(",")) if len(args) == 3 else None
    extracted, skipped = payload.extract_payload(payload_path, out_dir, only)
    if extracted:
        print(f"Extracted {len(extracted)} partition(s) to '{out_dir}':")
        for name, size in extracted:
            print(f"  {name:<24} {_fmt_bytes(size)}")
    if skipped:
        print("Skipped:")
        for name, reason in skipped:
            print(f"  {name:<24} {reason}")
    if not extracted and not skipped:
        print("No matching partitions found.")
        return 1
    return 0


def cmd_payload_create(args):
    if len(args) != 2:
        print("Usage: payload-create <partition_dir> <out_payload.bin>")
        return 1
    partition_dir, out_path = args
    if not os.path.isdir(partition_dir):
        print(f"'{partition_dir}' is not a folder")
        return 1
    partition_files = {}
    for fname in sorted(os.listdir(partition_dir)):
        if fname.lower().endswith(".img"):
            name = fname[:-4]
            partition_files[name] = os.path.join(partition_dir, fname)
    if not partition_files:
        print(f"No .img files found in '{partition_dir}'")
        return 1
    print(f"Compressing and packing {len(partition_files)} partition(s)...")
    info = payload.create_payload(partition_files, out_path)
    print(f"Wrote '{out_path}' ({_fmt_bytes(info['payload_size'])})")
    print(f"  Partitions: {', '.join(info['partitions'])}")
    print("  Note: this payload is UNSIGNED (minor_version 0, full payload).")
    print("  It is not a substitute for a vendor-signed OTA.")
    return 0


COMMANDS = {
    "img-unpack": cmd_img_unpack,
    "img-repack": cmd_img_repack,
    "super-unpack": cmd_super_unpack,
    "super-repack": cmd_super_repack,
    "super-list": cmd_super_list,
    "super-split": cmd_super_split,
    "super-merge": cmd_super_merge,
    "payload-list": cmd_payload_list,
    "payload-extract": cmd_payload_extract,
    "payload-create": cmd_payload_create,
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
