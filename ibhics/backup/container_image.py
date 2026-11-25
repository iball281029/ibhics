"""Read a stopped container's raw filesystem image without reading known file holes."""

import errno
import os
from pathlib import Path


class RawImageReader:
    def __init__(self, path):
        self.file = Path(path).open("rb", buffering=0)
        self.size = os.fstat(self.file.fileno()).st_size

    def close(self):
        self.file.close()

    def read(self, offset, count):
        return os.pread(self.file.fileno(), count, offset)

    def changed_chunks(self, chunk_size):
        offset, last = 0, -1
        while offset < self.size:
            try:
                data = os.lseek(self.file.fileno(), offset, os.SEEK_DATA)
            except (AttributeError, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno == errno.ENXIO:
                    return
                if isinstance(exc, OSError) and exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
                yield from range(last + 1, (self.size + chunk_size - 1) // chunk_size)
                return
            # ENXIO from SEEK_HOLE is not evidence that the remaining data is zero.
            hole = min(self.size, os.lseek(self.file.fileno(), data, os.SEEK_HOLE))
            if hole <= offset or hole <= data:
                raise RuntimeError("Invalid raw image allocation response")
            for index in range(data // chunk_size, (hole + chunk_size - 1) // chunk_size):
                if index > last:
                    yield index
                    last = index
            offset = hole
