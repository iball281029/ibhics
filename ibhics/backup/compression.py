"""Self-describing chunks; legacy zlib chunks remain readable."""

import zlib

ZSTD_MAGIC = b"IBZSTD1\0"
PROFILES = ("fast", "balanced", "maximum")


def codec(profile):
    if profile not in PROFILES:
        raise ValueError("Compression profile must be fast, balanced or maximum")
    try:
        import zstandard  # noqa: F401
    except ImportError:
        return {"fast": "zlib-1", "balanced": "zlib-6", "maximum": "zlib-9"}[profile]
    return {"fast": "zstd-1", "balanced": "zstd-3", "maximum": "zstd-19"}[profile]


def compress(block, profile):
    selected = codec(profile)
    level = int(selected.split("-")[1])
    if selected.startswith("zstd-"):
        import zstandard

        # A compressor belongs to this call; never share it across writer threads.
        return ZSTD_MAGIC + zstandard.ZstdCompressor(level=level).compress(block)
    return zlib.compress(block, level)


def decompress(data, limit):
    if data.startswith(ZSTD_MAGIC):
        try:
            import zstandard
        except ImportError:
            raise RuntimeError("Zstd restore requires ibhics[compression] on this node") from None
        payload = data[len(ZSTD_MAGIC) :]
        declared = zstandard.frame_content_size(payload)
        if declared > limit:
            raise RuntimeError("Backup chunk exceeds configured size")
        block = zstandard.ZstdDecompressor().decompress(payload, max_output_size=limit)
    else:
        obj = zlib.decompressobj()
        block = obj.decompress(data, limit + 1)
        if obj.unconsumed_tail or not obj.eof:
            raise RuntimeError("Invalid or oversized compressed backup chunk")
    if len(block) > limit:
        raise RuntimeError("Backup chunk exceeds configured size")
    return block
