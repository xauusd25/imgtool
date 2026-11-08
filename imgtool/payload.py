"""
payload.bin extractor / creator — Android A/B OTA update payloads.

payload.bin is the file inside Android OTA packages (full.zip / the
payload.bin entry of an OTA) that carries every partition image, in the
format produced by Google's update_engine ("update_metadata.proto").

File layout:
    "CrAU"                      4 bytes  magic
    file_format_version         8 bytes  big-endian uint64 (2)
    manifest_size                8 bytes  big-endian uint64
    manifest_signature_size     4 bytes  big-endian uint32  (version >= 2)
    manifest                    manifest_size bytes  (protobuf: DeltaArchiveManifest)
    manifest_signature          manifest_signature_size bytes
    <partition data blobs, referenced by offset/length from the manifest>

The manifest is a Protocol Buffers message. To keep this toolkit
dependency-free (no `protobuf` package required on the user's machine),
this module includes a small hand-rolled protobuf reader/writer that only
knows the handful of fields needed here — it is not a general protobuf
library.

Supported operation types for EXTRACTION: REPLACE, REPLACE_BZ, REPLACE_XZ,
ZERO — which covers the overwhelming majority of *full* OTA payloads.
Delta/incremental operation types (SOURCE_COPY, *_BSDIFF, PUFFDIFF, ...)
need the original source partition and are not supported; such payloads
should be extracted with the device's matching previous build, which this
standalone tool has no notion of.

payload.bin files produced by CREATION here are unsigned, minor_version 0
"full" payloads with every partition as a single REPLACE_XZ operation —
suitable for tools that read payload.bin directly (e.g. `fastboot update`,
third-party flashers, payload dumpers). They are not signed the way a
genuine OTA from a device vendor is, so a stock recovery's OTA applier
will not accept them.
"""

import hashlib
import io
import lzma
import os
import struct

try:
    import bz2
    _HAVE_BZ2 = True
except ImportError:
    _HAVE_BZ2 = False

MAGIC = b"CrAU"
BLOCK_SIZE = 4096

# InstallOperation.Type enum values we understand
OP_REPLACE = 0
OP_REPLACE_BZ = 1
OP_SOURCE_COPY = 4
OP_SOURCE_BSDIFF = 5
OP_ZERO = 6
OP_DISCARD = 7
OP_REPLACE_XZ = 8
OP_PUFFDIFF = 9
OP_BROTLI_BSDIFF = 10

OP_NAMES = {
    0: "REPLACE", 1: "REPLACE_BZ", 2: "MOVE", 3: "BSDIFF",
    4: "SOURCE_COPY", 5: "SOURCE_BSDIFF", 6: "ZERO", 7: "DISCARD",
    8: "REPLACE_XZ", 9: "PUFFDIFF", 10: "BROTLI_BSDIFF",
}

_SUPPORTED_EXTRACT_OPS = {OP_REPLACE, OP_REPLACE_BZ, OP_REPLACE_XZ, OP_ZERO}


# --------------------------------------------------------------------------
# Minimal protobuf wire-format reader
# --------------------------------------------------------------------------

def _read_varint(buf, i):
    result = 0
    shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7


def _parse_fields(data):
    """Return {field_number: [raw_value, ...]} for one protobuf message.

    raw_value is an int for varint/fixed32/fixed64 fields, or bytes for
    length-delimited fields (string / submessage / bytes).
    """
    fields = {}
    i, n = 0, len(data)
    while i < n:
        tag, i = _read_varint(data, i)
        field_num, wire_type = tag >> 3, tag & 0x7
        if wire_type == 0:
            val, i = _read_varint(data, i)
        elif wire_type == 1:
            val = int.from_bytes(data[i:i + 8], "little")
            i += 8
        elif wire_type == 2:
            length, i = _read_varint(data, i)
            val = data[i:i + length]
            i += length
        elif wire_type == 5:
            val = int.from_bytes(data[i:i + 4], "little")
            i += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type} (field {field_num})")
        fields.setdefault(field_num, []).append(val)
    return fields


def _parse_extent(raw):
    f = _parse_fields(raw)
    return {
        "start_block": f.get(1, [0])[0],
        "num_blocks": f.get(2, [0])[0],
    }


def _parse_operation(raw):
    f = _parse_fields(raw)
    return {
        "type": f.get(1, [OP_REPLACE])[0],
        "data_offset": f.get(2, [None])[0],
        "data_length": f.get(3, [None])[0],
        "dst_extents": [_parse_extent(e) for e in f.get(6, [])],
    }


def _parse_partition_info(raw):
    f = _parse_fields(raw)
    return {"size": f.get(1, [0])[0]}


def _parse_partition_update(raw):
    f = _parse_fields(raw)
    name = f.get(1, [b""])[0]
    if isinstance(name, bytes):
        name = name.decode("utf-8", "ignore")
    new_info = f.get(7, [None])[0]
    return {
        "name": name,
        "new_partition_info": _parse_partition_info(new_info) if new_info else None,
        "operations": [_parse_operation(o) for o in f.get(8, [])],
    }


def parse_manifest(manifest_bytes):
    f = _parse_fields(manifest_bytes)
    block_size = f.get(3, [BLOCK_SIZE])[0]
    partitions = [_parse_partition_update(p) for p in f.get(9, [])]
    return {"block_size": block_size, "partitions": partitions}


# --------------------------------------------------------------------------
# Minimal protobuf wire-format writer
# --------------------------------------------------------------------------

def _enc_varint(value):
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _enc_tag(field_num, wire_type):
    return _enc_varint((field_num << 3) | wire_type)


def _enc_varint_field(field_num, value):
    return _enc_tag(field_num, 0) + _enc_varint(value)


def _enc_bytes_field(field_num, data):
    return _enc_tag(field_num, 2) + _enc_varint(len(data)) + data


def _enc_extent(start_block, num_blocks):
    return _enc_varint_field(1, start_block) + _enc_varint_field(2, num_blocks)


def _enc_partition_info(size, digest):
    return _enc_varint_field(1, size) + _enc_bytes_field(2, digest)


def _enc_operation(op_type, data_offset, data_length, num_blocks, data_sha256):
    body = _enc_varint_field(1, op_type)
    body += _enc_varint_field(2, data_offset)
    body += _enc_varint_field(3, data_length)
    body += _enc_bytes_field(6, _enc_extent(0, num_blocks))
    body += _enc_bytes_field(8, data_sha256)
    return body


def _enc_partition_update(name, size, digest, op_bytes):
    body = _enc_bytes_field(1, name.encode("utf-8"))
    body += _enc_bytes_field(7, _enc_partition_info(size, digest))
    body += _enc_bytes_field(8, op_bytes)
    return body


def _enc_manifest(block_size, partition_entries, minor_version=0):
    body = _enc_varint_field(3, block_size)
    body += _enc_varint_field(8, minor_version)
    for entry in partition_entries:
        body += _enc_bytes_field(9, entry)
    return body


# --------------------------------------------------------------------------
# High-level header I/O
# --------------------------------------------------------------------------

def _read_header(f):
    magic = f.read(4)
    if magic != MAGIC:
        raise ValueError("Not a payload.bin (missing 'CrAU' magic)")
    version = struct.unpack(">Q", f.read(8))[0]
    manifest_size = struct.unpack(">Q", f.read(8))[0]
    sig_size = 0
    if version >= 2:
        sig_size = struct.unpack(">I", f.read(4))[0]
    manifest = f.read(manifest_size)
    if sig_size:
        f.read(sig_size)  # manifest signature, not needed for extraction
    blob_start = f.tell()
    return version, manifest, blob_start


def list_payload(payload_path):
    """Return [(name, size_bytes, num_operations, fully_supported), ...]."""
    with open(payload_path, "rb") as f:
        _version, manifest_bytes, _blob_start = _read_header(f)
    manifest = parse_manifest(manifest_bytes)
    out = []
    for part in manifest["partitions"]:
        size = part["new_partition_info"]["size"] if part["new_partition_info"] else None
        ops = part["operations"]
        supported = all(op["type"] in _SUPPORTED_EXTRACT_OPS for op in ops)
        out.append((part["name"], size, len(ops), supported))
    return out


def extract_payload(payload_path, out_dir, only_names=None):
    """Extract partitions from payload.bin into <out_dir>/<name>.img.

    Returns (extracted, skipped) where each is a list of
    (name, detail_string).
    """
    os.makedirs(out_dir, exist_ok=True)
    extracted, skipped = [], []
    with open(payload_path, "rb") as f:
        _version, manifest_bytes, blob_start = _read_header(f)
        manifest = parse_manifest(manifest_bytes)
        block_size = manifest["block_size"] or BLOCK_SIZE

        for part in manifest["partitions"]:
            name = part["name"]
            if only_names and name not in only_names:
                continue

            ops = part["operations"]
            unsupported = [OP_NAMES.get(o["type"], str(o["type"])) for o in ops
                            if o["type"] not in _SUPPORTED_EXTRACT_OPS]
            if unsupported:
                skipped.append((name, f"unsupported operation type(s): {', '.join(sorted(set(unsupported)))} "
                                        f"(this looks like a delta/incremental payload)"))
                continue
            if any(o["type"] == OP_REPLACE_BZ for o in ops) and not _HAVE_BZ2:
                skipped.append((name, "bz2 module unavailable"))
                continue

            declared_size = part["new_partition_info"]["size"] if part["new_partition_info"] else None

            out_path = os.path.join(out_dir, f"{name}.img")
            with open(out_path, "wb") as out:
                for op in ops:
                    dst_blocks = sum(e["num_blocks"] for e in op["dst_extents"])
                    out_len = dst_blocks * block_size

                    if op["type"] == OP_ZERO:
                        data = b"\x00" * out_len
                    else:
                        f.seek(blob_start + op["data_offset"])
                        raw = f.read(op["data_length"])
                        if op["type"] == OP_REPLACE:
                            data = raw
                        elif op["type"] == OP_REPLACE_BZ:
                            data = bz2.decompress(raw)
                        elif op["type"] == OP_REPLACE_XZ:
                            data = lzma.decompress(raw)
                        else:
                            raise AssertionError("unreachable: filtered above")
                        if len(data) < out_len:
                            data = data + b"\x00" * (out_len - len(data))
                        data = data[:out_len]

                    # Place data per-extent at its real block offset — operations
                    # and their extents are not guaranteed to be contiguous/in order.
                    pos = 0
                    for ext in op["dst_extents"]:
                        ext_len = ext["num_blocks"] * block_size
                        out.seek(ext["start_block"] * block_size)
                        out.write(data[pos:pos + ext_len])
                        pos += ext_len

                if declared_size:
                    out.seek(0, os.SEEK_END)
                    if out.tell() < declared_size:
                        out.truncate(declared_size)
            extracted.append((name, os.path.getsize(out_path)))

    with open(os.path.join(out_dir, "partitions.txt"), "w") as f:
        f.write("# partition_name\tsize_bytes\n")
        for name, size in extracted:
            f.write(f"{name}\t{size}\n")

    return extracted, skipped


def create_payload(partition_files, out_path, block_size=BLOCK_SIZE):
    """
    Build an unsigned, minor_version-0 "full" payload.bin.

    partition_files: dict {partition_name: path_to_raw_img}
    Every partition is written as a single REPLACE_XZ operation covering
    the whole (block_size-padded) partition.
    """
    partition_entries = []
    blob_chunks = []
    blob_offset = 0

    for name in sorted(partition_files):
        path = partition_files[name]
        with open(path, "rb") as pf:
            raw = pf.read()

        pad = (-len(raw)) % block_size
        if pad:
            raw += b"\x00" * pad
        num_blocks = len(raw) // block_size

        digest = hashlib.sha256(raw).digest()
        compressed = lzma.compress(raw, preset=6)

        op_bytes = _enc_operation(
            OP_REPLACE_XZ, blob_offset, len(compressed), num_blocks, hashlib.sha256(compressed).digest()
        )
        partition_entries.append(_enc_partition_update(name, len(raw), digest, op_bytes))
        blob_chunks.append(compressed)
        blob_offset += len(compressed)

    manifest = _enc_manifest(block_size, partition_entries, minor_version=0)

    with open(out_path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack(">Q", 2))             # file_format_version
        f.write(struct.pack(">Q", len(manifest)))  # manifest_size
        f.write(struct.pack(">I", 0))              # manifest_signature_size (unsigned)
        f.write(manifest)
        for chunk in blob_chunks:
            f.write(chunk)

    return {
        "partitions": sorted(partition_files),
        "manifest_size": len(manifest),
        "payload_size": os.path.getsize(out_path),
    }
