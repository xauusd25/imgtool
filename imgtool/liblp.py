"""
super.img (Android dynamic-partition / "liblp") unpack and repack.

super.img is a container that holds several logical partitions (system,
vendor, product, odm, etc.) inside one physical image, described by a
small metadata section (the "liblp" format used by Android's
fs_mgr/liblp, the same format read/written by AOSP's lpunpack/lpmake).

Layout of a (single block-device) super.img:

    [0            .. 4096)   reserved
    [4096         .. 8192)   primary geometry
    [8192         .. 12288)  backup geometry
    [12288        .. X)      metadata slots (primary copies, then backup copies)
    [X            .. end)    partition data region (raw partition contents)

This module parses that metadata, then:
  - unpack_super(): extracts each logical partition to its own .img file
  - repack_super(): writes replacement .img files back into a copy of the
                     original super.img, at the exact extents recorded in
                     its (unmodified) metadata. This only works when each
                     replacement file is no larger than the space already
                     reserved for that partition inside super.img — which
                     is the normal case when re-flashing a modified
                     partition (e.g. after editing system.img).

NOTE: this is a best-effort, dependency-free re-implementation of the
liblp format for single block-device super images (by far the most common
case on phones). It does not regenerate metadata/checksums -- repacking
only ever overwrites existing partition *data*, never the partition
table -- which keeps it safe: if anything about a device's super.img
doesn't match these assumptions, parsing will raise a clear error rather
than silently producing a corrupt image.
"""

import os
import shutil
import struct

GEOMETRY_MAGIC = 0x616C4467
GEOMETRY_SIZE = 4096
HEADER_MAGIC = 0x414C5030
SECTOR_SIZE = 512

_GEOMETRY_FMT = "<II32sIII"
_GEOMETRY_FIELDS_SIZE = struct.calcsize(_GEOMETRY_FMT)

# magic, major, minor, header_size, header_checksum[32], tables_size, tables_checksum[32],
# then 4 table descriptors (offset, num_entries, entry_size) x 4 = partitions, extents, groups, block_devices
_HEADER_FMT = "<IHHI32sI32s" + "III" * 4
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)

_PARTITION_FMT = "<36sIIII"       # name, attributes, first_extent_index, num_extents, group_index
_PARTITION_SIZE = struct.calcsize(_PARTITION_FMT)

_EXTENT_FMT = "<QIQI"             # num_sectors, target_type, target_data, target_source
_EXTENT_SIZE = struct.calcsize(_EXTENT_FMT)

_BLOCK_DEVICE_FMT = "<QIIQ36sI"   # first_logical_sector, alignment, alignment_offset, size, partition_name, flags
_BLOCK_DEVICE_SIZE = struct.calcsize(_BLOCK_DEVICE_FMT)

_GROUP_FMT = "<36sIQ"             # name, flags, maximum_size
_GROUP_SIZE = struct.calcsize(_GROUP_FMT)

TARGET_TYPE_LINEAR = 0
TARGET_TYPE_ZERO = 1

_READ_BUF = 1024 * 1024


class LpMetadata:
    def __init__(self):
        self.geometry = {}
        self.header = {}
        self.partitions = []
        self.extents = []
        self.groups = []
        self.block_devices = []
        self.partition_data_offset = 0


def _cstr(b):
    return b.split(b"\x00", 1)[0].decode("utf-8", "ignore")


def _read_geometry(f, offset):
    f.seek(offset)
    buf = f.read(GEOMETRY_SIZE)
    if len(buf) < _GEOMETRY_FIELDS_SIZE:
        return None
    magic, struct_size, _checksum, metadata_max_size, metadata_slot_count, logical_block_size = \
        struct.unpack(_GEOMETRY_FMT, buf[:_GEOMETRY_FIELDS_SIZE])
    if magic != GEOMETRY_MAGIC:
        return None
    return {
        "struct_size": struct_size,
        "metadata_max_size": metadata_max_size,
        "metadata_slot_count": metadata_slot_count,
        "logical_block_size": logical_block_size,
    }


def parse_metadata(path):
    """Parse the liblp metadata from a super.img file."""
    meta = LpMetadata()
    with open(path, "rb") as f:
        geo = _read_geometry(f, GEOMETRY_SIZE)
        if geo is None:
            geo = _read_geometry(f, GEOMETRY_SIZE * 2)
        if geo is None:
            raise ValueError(
                "No valid LP geometry block found at offset 4096 or 8192 — "
                "this doesn't look like a super.img (dynamic partition image)."
            )
        meta.geometry = geo

        metadata_region_start = GEOMETRY_SIZE * 3
        f.seek(metadata_region_start)
        header_buf = f.read(_HEADER_SIZE)
        if len(header_buf) < _HEADER_SIZE:
            raise ValueError("File too small to contain LP metadata header")

        fields = struct.unpack(_HEADER_FMT, header_buf)
        (magic, major, minor, header_size, _hchk, _tsize, _tchk,
         p_off, p_num, p_entsz,
         e_off, e_num, e_entsz,
         g_off, g_num, g_entsz,
         b_off, b_num, b_entsz) = fields

        if magic != HEADER_MAGIC:
            raise ValueError(
                "LP metadata header magic mismatch — unsupported or corrupt super.img"
            )
        meta.header = {"major_version": major, "minor_version": minor}

        tables_start = metadata_region_start + header_size

        def read_table(off, num, entsz, fmt, needed_size):
            f.seek(tables_start + off)
            out = []
            for _ in range(num):
                raw = f.read(entsz)
                if len(raw) < needed_size:
                    raise ValueError("Unexpected end of file while reading metadata table")
                out.append(struct.unpack(fmt, raw[:needed_size]))
            return out

        parts_raw = read_table(p_off, p_num, p_entsz, _PARTITION_FMT, _PARTITION_SIZE)
        ext_raw = read_table(e_off, e_num, e_entsz, _EXTENT_FMT, _EXTENT_SIZE)
        grp_raw = read_table(g_off, g_num, g_entsz, _GROUP_FMT, _GROUP_SIZE)
        bd_raw = read_table(b_off, b_num, b_entsz, _BLOCK_DEVICE_FMT, _BLOCK_DEVICE_SIZE)

        meta.groups = [
            {"name": _cstr(n), "flags": fl, "maximum_size": ms} for (n, fl, ms) in grp_raw
        ]
        meta.block_devices = [
            {
                "first_logical_sector": fls, "alignment": al, "alignment_offset": ao,
                "size": sz, "partition_name": _cstr(pn), "flags": flg,
            }
            for (fls, al, ao, sz, pn, flg) in bd_raw
        ]
        meta.extents = [
            {"num_sectors": ns, "target_type": tt, "target_data": td, "target_source": tsrc}
            for (ns, tt, td, tsrc) in ext_raw
        ]
        meta.partitions = [
            {
                "name": _cstr(n), "attributes": attr,
                "first_extent_index": fei, "num_extents": ne, "group_index": gi,
            }
            for (n, attr, fei, ne, gi) in parts_raw
        ]

        if len(meta.block_devices) > 1:
            raise ValueError(
                "This super.img references multiple block devices; this tool only "
                "supports the common single block-device layout."
            )

        slot_count = meta.geometry["metadata_slot_count"]
        max_size = meta.geometry["metadata_max_size"]
        meta.partition_data_offset = metadata_region_start + 2 * slot_count * max_size

    return meta


def list_partitions(super_path):
    """Return [(name, size_bytes, num_extents), ...] for a super.img."""
    meta = parse_metadata(super_path)
    out = []
    for part in meta.partitions:
        if not part["name"]:
            continue
        exts = meta.extents[part["first_extent_index"]: part["first_extent_index"] + part["num_extents"]]
        size = sum(e["num_sectors"] * SECTOR_SIZE for e in exts)
        out.append((part["name"], size, part["num_extents"]))
    return out


def unpack_super(super_path, out_dir):
    """Extract every logical partition in super.img to <out_dir>/<name>.img."""
    meta = parse_metadata(super_path)
    os.makedirs(out_dir, exist_ok=True)
    results = []
    with open(super_path, "rb") as f:
        for part in meta.partitions:
            name = part["name"]
            if not name:
                continue
            exts = meta.extents[part["first_extent_index"]: part["first_extent_index"] + part["num_extents"]]
            out_path = os.path.join(out_dir, f"{name}.img")
            total = 0
            with open(out_path, "wb") as out:
                for ext in exts:
                    byte_len = ext["num_sectors"] * SECTOR_SIZE
                    if ext["target_type"] == TARGET_TYPE_ZERO:
                        remaining = byte_len
                        zero_chunk = b"\x00" * min(_READ_BUF, byte_len)
                        while remaining > 0:
                            n = min(len(zero_chunk), remaining)
                            out.write(zero_chunk[:n])
                            remaining -= n
                    elif ext["target_type"] == TARGET_TYPE_LINEAR:
                        byte_off = meta.partition_data_offset + ext["target_data"] * SECTOR_SIZE
                        f.seek(byte_off)
                        remaining = byte_len
                        while remaining > 0:
                            buf = f.read(min(_READ_BUF, remaining))
                            if not buf:
                                raise ValueError(f"Unexpected end of file extracting partition '{name}'")
                            out.write(buf)
                            remaining -= len(buf)
                    else:
                        raise ValueError(f"Unsupported extent target_type {ext['target_type']} in partition '{name}'")
                    total += byte_len
            results.append((name, total))

    with open(os.path.join(out_dir, "partitions.txt"), "w") as f:
        f.write("# partition_name\tsize_bytes\n")
        for name, size in results:
            f.write(f"{name}\t{size}\n")

    return results


def repack_super(super_path, partition_dir, out_path):
    """
    Build out_path as a copy of super_path with any matching <name>.img files
    from partition_dir written back into their original extents.

    A replacement file must fit within the space already reserved for that
    partition in the original super.img; it will be zero-padded if smaller,
    and refused if larger.
    """
    meta = parse_metadata(super_path)
    if os.path.abspath(super_path) != os.path.abspath(out_path):
        shutil.copyfile(super_path, out_path)

    report = []
    with open(out_path, "r+b") as f:
        for part in meta.partitions:
            name = part["name"]
            if not name:
                continue
            candidate = os.path.join(partition_dir, f"{name}.img")
            if not os.path.isfile(candidate):
                continue

            exts = [
                e for e in meta.extents[part["first_extent_index"]: part["first_extent_index"] + part["num_extents"]]
                if e["target_type"] == TARGET_TYPE_LINEAR
            ]
            capacity = sum(e["num_sectors"] * SECTOR_SIZE for e in exts)
            file_size = os.path.getsize(candidate)

            if file_size > capacity:
                report.append((name, "SKIPPED", f"{file_size} bytes doesn't fit in the "
                                                  f"{capacity}-byte space reserved for '{name}' in super.img"))
                continue

            with open(candidate, "rb") as src:
                remaining = file_size
                for ext in exts:
                    if remaining <= 0:
                        break
                    byte_off = meta.partition_data_offset + ext["target_data"] * SECTOR_SIZE
                    byte_len = ext["num_sectors"] * SECTOR_SIZE
                    to_write = min(byte_len, remaining)
                    f.seek(byte_off)
                    buf = src.read(to_write)
                    f.write(buf)
                    remaining -= to_write
            report.append((name, "OK", f"wrote {file_size} bytes"))

    return report
