"""Convert libvirt's P6 console screenshots to PNG without optional dependencies."""

import re
import struct
import zlib


def console_png(data: bytes, mime: str) -> bytes:
    if mime == "image/png":
        return data
    if mime != "image/x-portable-pixmap":
        raise ValueError(f"Unsupported console image format: {mime}")
    position, tokens = 0, []
    for _ in range(4):
        match = re.compile(rb"(?:\s|#[^\r\n]*(?:\r?\n))*([^\s#]+)").match(data, position)
        if not match:
            raise ValueError("Invalid PPM header")
        tokens.append(match[1])
        position = match.end()
    if tokens[0] != b"P6" or tokens[3] != b"255":
        raise ValueError("Only 8-bit P6 screenshots are supported")
    width, height = int(tokens[1]), int(tokens[2])
    if not (0 < width <= 8192 and 0 < height <= 8192 and width * height <= 16777216):
        raise ValueError("Console screenshot dimensions exceed limit")
    delimiter = 2 if data[position : position + 2] == b"\r\n" else 1
    if data[position : position + 1] not in (b" ", b"\t", b"\r", b"\n"):
        raise ValueError("Missing PPM raster delimiter")
    pixels = data[position + delimiter :]
    if len(pixels) != width * height * 3:
        raise ValueError("Invalid PPM raster length")

    def chunk(kind, payload):
        body = kind + payload
        return struct.pack("!I", len(payload)) + body + struct.pack("!I", zlib.crc32(body))

    stride = width * 3
    raster = b"".join(b"\0" + pixels[i : i + stride] for i in range(0, len(pixels), stride))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raster))
        + chunk(b"IEND", b"")
    )
