"""A tiny VNC (RFB 3.8) server that shows a test picture, for fake VMs.

It lets the console relay and the browser console be tried on a laptop without KVM.
It speaks just enough of the protocol for noVNC: no authentication, raw encoding,
whole-screen updates, input ignored.
"""

from __future__ import annotations

import socketserver
import struct
import threading

WIDTH, HEIGHT = 800, 480


def _picture(r_shift: int, g_shift: int, b_shift: int, big_endian: bool, seed: int) -> bytes:
    """A gradient with a block whose colour depends on the VM, in the client's pixel format."""
    order = ">" if big_endian else "<"
    pack = struct.Struct(f"{order}I").pack
    accent = ((seed * 67) % 200 + 40, (seed * 151) % 200 + 40, 200)
    rows = []
    for y in range(HEIGHT):
        row = bytearray()
        for x in range(WIDTH):
            if 240 <= x < 560 and 160 <= y < 320:
                r, g, b = accent
            else:
                r, g, b = 16 + x * 40 // WIDTH, 24 + y * 40 // HEIGHT, 48 + y * 60 // HEIGHT
            row += pack((r << r_shift) | (g << g_shift) | (b << b_shift))
        rows.append(bytes(row))
    return b"".join(rows)


class _Handler(socketserver.BaseRequestHandler):
    def _recv(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            part = self.request.recv(n - len(data))
            if not part:
                raise ConnectionError
            data += part
        return data

    def handle(self) -> None:
        sock = self.request
        name = self.server.desktop_name.encode()
        try:
            sock.sendall(b"RFB 003.008\n")
            self._recv(12)
            sock.sendall(b"\x01\x01")  # one security type: None
            self._recv(1)
            sock.sendall(struct.pack(">I", 0))  # security result: OK
            self._recv(1)  # ClientInit
            # 32 bpp, depth 24, little-endian, true colour, RGB 8 bits each at 16/8/0.
            fmt = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
            sock.sendall(struct.pack(">HH", WIDTH, HEIGHT) + fmt + struct.pack(">I", len(name)))
            sock.sendall(name)
            shifts, big_endian, frame = (16, 8, 0), False, None
            while True:
                kind = self._recv(1)[0]
                if kind == 0:  # SetPixelFormat
                    pf = self._recv(19)[3:]
                    big_endian = bool(pf[2])
                    shifts = (pf[10], pf[11], pf[12])
                    frame = None
                elif kind == 2:  # SetEncodings
                    (count,) = struct.unpack(">xH", self._recv(3))
                    self._recv(4 * count)
                elif kind == 3:  # FramebufferUpdateRequest
                    incremental = self._recv(9)[0]
                    if incremental:
                        continue  # the picture never changes
                    if frame is None:
                        frame = _picture(*shifts, big_endian, self.server.seed)
                    header = struct.pack(">BxH", 0, 1)
                    rect = struct.pack(">HHHHi", 0, 0, WIDTH, HEIGHT, 0)
                    sock.sendall(header + rect + frame)
                elif kind == 4:  # KeyEvent
                    self._recv(7)
                elif kind == 5:  # PointerEvent
                    self._recv(5)
                elif kind == 6:  # ClientCutText
                    (length,) = struct.unpack(">3xI", self._recv(7))
                    self._recv(length)
                else:
                    return
        except (ConnectionError, OSError):
            return


class FakeVNCServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, desktop_name: str, seed: int = 0) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.desktop_name = desktop_name
        self.seed = seed
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def address(self) -> tuple[str, int]:
        return self.server_address[0], self.server_address[1]
