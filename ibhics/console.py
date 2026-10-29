"""Relay a browser's noVNC WebSocket to a VM's VNC display.

The VNC server of every VM listens on 127.0.0.1 of its node only. The manager relays the
browser's WebSocket straight to it when the VM runs on the manager's own node, or to the
agent of the VM's node, which relays the last hop the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect

CHUNK = 64 * 1024
REFUSED = 4000  # WebSocket close code meaning "no console", with the reason as text


async def accept(ws: WebSocket) -> None:
    # noVNC asks for the "binary" subprotocol; echo it back when offered.
    subprotocol = "binary" if "binary" in ws.scope.get("subprotocols", []) else None
    await ws.accept(subprotocol=subprotocol)


async def _browser_frames(ws: WebSocket, send: Callable[[bytes], Awaitable[None]]) -> None:
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is None:
                data = (message.get("text") or "").encode()
            await send(data)
    except WebSocketDisconnect:
        return


async def _run_both(*coros) -> None:
    """Run the two directions of a relay; when one side ends, stop the other."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def bridge_tcp(ws: WebSocket, host: str, port: int) -> None:
    """Relay an accepted WebSocket to a TCP endpoint until either side closes."""
    reader, writer = await asyncio.open_connection(host, port)

    async def to_vnc(data: bytes) -> None:
        writer.write(data)
        await writer.drain()

    async def from_vnc() -> None:
        while data := await reader.read(CHUNK):
            await ws.send_bytes(data)

    try:
        await _run_both(_browser_frames(ws, to_vnc), from_vnc())
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        with contextlib.suppress(Exception):
            await ws.close()


async def bridge_ws(ws: WebSocket, remote) -> None:
    """Relay an accepted WebSocket to another WebSocket connection (an agent's console).

    `remote` needs async `send(bytes)` and async iteration over incoming messages, which
    is what a `websockets` client connection provides.
    """

    async def from_agent() -> None:
        async for message in remote:
            await ws.send_bytes(message if isinstance(message, bytes) else message.encode())

    try:
        await _run_both(_browser_frames(ws, remote.send), from_agent())
    finally:
        with contextlib.suppress(Exception):
            if getattr(remote, "close_code", None) == REFUSED:
                await ws.close(code=REFUSED, reason=remote.close_reason or "")
            else:
                await ws.close()


async def refuse(ws: WebSocket, reason: str) -> None:
    """Tell the browser why there is no console."""
    with contextlib.suppress(Exception):  # the socket may already be closed
        await ws.close(code=REFUSED, reason=reason[:120])
