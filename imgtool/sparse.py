"""
Android sparse image (.img) unpack/repack.

Implements the Android sparse image format used by fastboot/ADB images
(the same format handled by AOSP's simg2img / img2simg tools):

  - unsparse(): sparse image -> raw (flat) image   ["unpack .img"]
  - resparse(): raw (flat) image -> sparse image   ["repack .img"]

Format reference: system/core/libsparse (AOSP).
"""

import os
import struct

SPARSE_HEADER_MAGIC = 0xED26FF3A

HEADER_FMT = "<IHHHHIIII"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 28

CHUNK_HEADER_FMT = "<HHII"
CHUNK_HEADER_SIZE = struct.calcsize(CHUNK_HEADER_FMT)  # 12

CHUNK_TYPE_RAW = 0xCAC1
CHUNK_TYPE_FILL = 0xCAC2
CHUNK_TYPE_DONT_CARE = 0xCAC3
CHUNK_TYPE_CRC32 = 0xCAC4

_READ_BUF = 1024 * 1024


def is_sparse(path):
    """Return True if the file at `path` starts with a sparse image header."""
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError:
        return False
    return len(magic) == 4 and struct.unpack("<I", magic)[0] == SPARSE_HEADER_MAGIC


def unsparse(src, dst):
    """Convert an Android sparse image at `src` into a raw flat image at `dst`."""
    with open(src, "rb") as f_in, open(dst, "wb") as f_out:
        header = f_in.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE:
            raise ValueError("File is too small to be a sparse image")

        (magic, major, minor, file_hdr_sz, chunk_hdr_sz,
         blk_sz, total_blks, total_chunks, _checksum) = struct.unpack(HEADER_FMT, header)

        if magic != SPARSE_HEADER_MAGIC:
            raise ValueError("Not a sparse image (bad magic) — nothing to unpack")

        if file_hdr_sz > HEADER_SIZE:
            f_in.read(file_hdr_sz - HEADER_SIZE)

        for _ in range(total_chunks):
            ch = f_in.read(chunk_hdr_sz)
            if len(ch) < CHUNK_HEADER_SIZE:
                raise ValueError("Unexpected end of file while reading a chunk header")
            chunk_type, _reserved1, chunk_sz, total_sz = struct.unpack(
                CHUNK_HEADER_FMT, ch[:CHUNK_HEADER_SIZE]
            )
            if chunk_hdr_sz > CHUNK_HEADER_SIZE:
                f_in.read(chunk_hdr_sz - CHUNK_HEADER_SIZE)

            chunk_bytes = chunk_sz * blk_sz

            if chunk_type == CHUNK_TYPE_RAW:
                remaining = chunk_bytes
                while remaining > 0:
                    buf = f_in.read(min(_READ_BUF, remaining))
                    if not buf:
                        raise ValueError("Unexpected end of file inside a raw chunk")
                    f_out.write(buf)
                    remaining -= len(buf)

            elif chunk_type == CHUNK_TYPE_FILL:
                fill_word = f_in.read(4)
                if len(fill_word) < 4:
                    raise ValueError("Unexpected end of file reading fill value")
                block = fill_word * (blk_sz // 4)
                for _ in range(chunk_sz):
                    f_out.write(block)

            elif chunk_type == CHUNK_TYPE_DONT_CARE:
                # Hole: skip forward without writing (sparse on NTFS/most filesystems).
                f_out.seek(chunk_bytes, os.SEEK_CUR)

            elif chunk_type == CHUNK_TYPE_CRC32:
                f_in.read(4)

            else:
                raise ValueError(f"Unknown sparse chunk type: 0x{chunk_type:x}")

        f_out.truncate(total_blks * blk_sz)


def resparse(src, dst, blk_sz=4096):
    """Convert a raw flat image at `src` into an Android sparse image at `dst`."""
    size = os.path.getsize(src)
    if size % blk_sz != 0:
        raise ValueError(
            f"Raw image size ({size} bytes) is not a multiple of the block size "
            f"({blk_sz}); pad the file first or pass a matching block size."
        )
    total_blks = size // blk_sz
    zero_block = b"\x00" * blk_sz

    # Pass 1: build a run-length list of (type, start_blk, num_blks)
    chunks = []
    cur_type, cur_start, cur_len = None, 0, 0
    with open(src, "rb") as f_in:
        for i in range(total_blks):
            block = f_in.read(blk_sz)
            t = CHUNK_TYPE_DONT_CARE if block == zero_block else CHUNK_TYPE_RAW
            if t == cur_type:
                cur_len += 1
            else:
                if cur_type is not None:
                    chunks.append((cur_type, cur_start, cur_len))
                cur_type, cur_start, cur_len = t, i, 1
        if cur_type is not None:
            chunks.append((cur_type, cur_start, cur_len))

    # Pass 2: write the sparse file
    with open(dst, "wb") as f_out, open(src, "rb") as f_in:
        header = struct.pack(
            HEADER_FMT,
            SPARSE_HEADER_MAGIC, 1, 0, HEADER_SIZE, CHUNK_HEADER_SIZE,
            blk_sz, total_blks, len(chunks), 0,
        )
        f_out.write(header)
        for (t, start, length) in chunks:
            if t == CHUNK_TYPE_RAW:
                total_sz = CHUNK_HEADER_SIZE + length * blk_sz
                f_out.write(struct.pack(CHUNK_HEADER_FMT, t, 0, length, total_sz))
                f_in.seek(start * blk_sz)
                remaining = length * blk_sz
                while remaining > 0:
                    buf = f_in.read(min(_READ_BUF, remaining))
                    f_out.write(buf)
                    remaining -= len(buf)
            else:  # DONT_CARE hole
                f_out.write(struct.pack(CHUNK_HEADER_FMT, t, 0, length, CHUNK_HEADER_SIZE))

    return len(chunks)
