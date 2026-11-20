"""Which switch port each network port of this node is cabled to, from LLDP.

Switches announce themselves every 30 s or so (LLDP, ethertype 0x88cc). One raw
socket listens on every interface and keeps the latest announcement per port, so
asking costs nothing; nothing is sent. Without the permission to open raw sockets
(or on a system without them) the list stays empty.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

ETH_P_LLDP = 0x88CC
_lock = threading.Lock()
_seen: dict[str, dict] = {}
_thread: threading.Thread | None = None


def parse(frame: bytes) -> dict:
    """The fields of an LLDP frame (Ethernet header included) worth showing."""
    out: dict = {}
    data, i = frame[14:], 0
    while i + 2 <= len(data):
        head = struct.unpack("!H", data[i : i + 2])[0]
        kind, size = head >> 9, head & 0x1FF
        value = data[i + 2 : i + 2 + size]
        i += 2 + size
        if kind == 0:
            break
        if kind == 1 and value:  # chassis ID
            out["chassis_id"] = (
                ":".join(f"{b:02x}" for b in value[1:])
                if value[0] == 4
                else value[1:].decode(errors="replace")
            )
        elif kind == 2 and value:
            out["port_id"] = value[1:].decode(errors="replace")
        elif kind in (4, 5, 6):
            key = {4: "port_desc", 5: "system_name", 6: "system_desc"}[kind]
            out[key] = value.decode(errors="replace").strip()[:200]
        elif kind == 8 and len(value) > 2 and value[1] == 1 and value[0] >= 5:  # IPv4 management
            out["management"] = socket.inet_ntoa(value[2:6])
    return out


def _listen() -> None:
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_LLDP))
    except (OSError, AttributeError):
        return  # no raw sockets here: nothing to learn
    while True:
        try:
            frame, address = sock.recvfrom(4096)
            info = parse(frame)
        except Exception:
            time.sleep(1)
            continue
        if info:
            with _lock:
                _seen[address[0]] = {**info, "interface": address[0], "seen": time.time()}


def neighbors(max_age: float = 600) -> list[dict]:
    """Switch ports seen on this node's interfaces in the last `max_age` seconds."""
    global _thread
    if _thread is None:
        _thread = threading.Thread(target=_listen, daemon=True, name="lldp")
        _thread.start()
    now = time.time()
    with _lock:
        return sorted(
            (dict(v) for v in _seen.values() if now - v["seen"] <= max_age),
            key=lambda v: v["interface"],
        )


def start() -> None:
    """Begin listening early (at agent start), so the first answer is not empty."""
    neighbors()
