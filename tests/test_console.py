import asyncio
import contextlib
import socketserver
import threading
import types

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ibhics.agent import create_agent_app
from ibhics.cluster import Cluster, NodeRole
from ibhics.hypervisor import InvalidState
from ibhics.hypervisor import libvirt_driver as lv
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app

TOKEN = "s3cret"


class Echo(socketserver.BaseRequestHandler):
    """Stands in for a VNC server: sends a greeting, then echoes."""

    def handle(self):
        self.request.sendall(b"RFB 003.008\n")
        while data := self.request.recv(4096):
            self.request.sendall(data)


@pytest.fixture
def vnc():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address
    server.shutdown()
    server.server_close()


def running_vm(driver, name="web"):
    from ibhics.hypervisor import VMSpec

    vm = driver.create_vm(VMSpec(name=name))
    driver.start_vm(vm.uuid)
    return vm.uuid


def test_console_relays_local_vm(vnc):
    driver = FakeDriver()
    uuid = running_vm(driver)
    driver.consoles[uuid] = vnc
    client = TestClient(create_app(driver))
    url = f"/api/v1/vms/{uuid}/console"
    with client.websocket_connect(url, subprotocols=["binary"]) as ws:
        assert ws.accepted_subprotocol == "binary"
        assert ws.receive_bytes() == b"RFB 003.008\n"
        ws.send_bytes(b"\x01\x02keys")
        assert ws.receive_bytes() == b"\x01\x02keys"


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("stopped", "start the VM"),
        ("no-display", "no VNC display"),
        ("missing", "VM not found"),
    ],
)
def test_console_refusals_say_why(setup, reason):
    driver = FakeDriver(demo_console=False)
    uuid = running_vm(driver)
    if setup == "stopped":
        driver.stop_vm(uuid, force=True)
    if setup == "missing":
        uuid = "00000000-0000-0000-0000-000000000000"
    client = TestClient(create_app(driver))
    with client.websocket_connect(f"/api/v1/vms/{uuid}/console") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_bytes()
    assert exc.value.code == 4000 and reason in exc.value.reason


def test_unreachable_display():
    driver = FakeDriver()
    uuid = running_vm(driver)
    driver.consoles[uuid] = ("127.0.0.1", 1)  # nothing listens there
    client = TestClient(create_app(driver))
    with client.websocket_connect(f"/api/v1/vms/{uuid}/console") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_bytes()
    assert exc.value.code == 4000


def test_agent_console_needs_token(vnc):
    driver = FakeDriver()
    uuid = running_vm(driver)
    driver.consoles[uuid] = vnc
    agent = TestClient(create_agent_app("node2", NodeRole.COMPUTE, TOKEN, driver))
    url = f"/agent/v1/vms/{uuid}/console"
    with pytest.raises(WebSocketDisconnect) as exc:
        with agent.websocket_connect(url, headers={"X-Ibhics-Token": "wrong"}) as ws:
            ws.receive_bytes()
    assert exc.value.code == 1008

    with agent.websocket_connect(url, headers={"X-Ibhics-Token": TOKEN}) as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"
        ws.send_bytes(b"ping")
        assert ws.receive_bytes() == b"ping"


class FakeAgentConsole:
    """What AgentClient.open_console yields: a websockets-style connection."""

    def __init__(self, refuse=None):
        self.queue = asyncio.Queue()
        self.close_code = 4000 if refuse else None
        self.close_reason = refuse
        if not refuse:
            self.queue.put_nowait(b"RFB 003.008\n")

    async def send(self, data):
        await self.queue.put(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.close_code:
            raise StopAsyncIteration
        return await self.queue.get()


def remote_cluster(agent_console):
    cluster = Cluster(token=TOKEN)
    cluster.add_local("node1", FakeDriver())
    remote = FakeDriver()
    uuid = running_vm(remote, "app")

    @contextlib.asynccontextmanager
    async def open_console(vm_uuid):
        assert vm_uuid == uuid
        yield agent_console

    client = types.SimpleNamespace(open_console=open_console)
    member = types.SimpleNamespace(
        name="node2", local=False, driver=remote, client=client, role=NodeRole.COMPUTE
    )
    cluster.find_vm = lambda u: (member, remote.get_vm(u))
    return cluster, uuid


def test_console_relays_through_agent():
    cluster, uuid = remote_cluster(FakeAgentConsole())
    client = TestClient(create_app(cluster))
    with client.websocket_connect(f"/api/v1/vms/{uuid}/console") as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"
        ws.send_bytes(b"hello")
        assert ws.receive_bytes() == b"hello"


def test_agent_refusal_reaches_browser():
    cluster, uuid = remote_cluster(FakeAgentConsole(refuse="the VM has no VNC display"))
    client = TestClient(create_app(cluster))
    with client.websocket_connect(f"/api/v1/vms/{uuid}/console") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_bytes()
    assert (exc.value.code, exc.value.reason) == (4000, "the VM has no VNC display")


def test_libvirt_console_address(monkeypatch):
    monkeypatch.setattr(lv, "libvirt", types.SimpleNamespace(libvirtError=Exception))

    class Dom:
        def __init__(self, active, port):
            self.active, self.port = active, port

        def isActive(self):
            return self.active

        def XMLDesc(self, _flags):
            return (
                f"<domain><devices><graphics type='vnc' port='{self.port}' "
                "listen='127.0.0.1'/></devices></domain>"
            )

    driver = object.__new__(lv.LibvirtDriver)
    for dom, expected in [(Dom(True, 5901), ("127.0.0.1", 5901))]:
        driver._conn = types.SimpleNamespace(lookupByUUIDString=lambda _u, d=dom: d)
        assert driver.console_address("u") == expected
    driver._conn = types.SimpleNamespace(lookupByUUIDString=lambda _u: Dom(False, 5901))
    with pytest.raises(InvalidState, match="start"):
        driver.console_address("u")
    driver._conn = types.SimpleNamespace(lookupByUUIDString=lambda _u: Dom(True, -1))
    with pytest.raises(InvalidState, match="no VNC"):
        driver.console_address("u")


def test_demo_console_speaks_rfb():
    import struct

    driver = FakeDriver()
    uuid = running_vm(driver)
    client = TestClient(create_app(driver))
    with client.websocket_connect(f"/api/v1/vms/{uuid}/console") as ws:
        buffer = b""

        def recv(n):
            nonlocal buffer
            while len(buffer) < n:
                buffer += ws.receive_bytes()
            data, buffer = buffer[:n], buffer[n:]
            return data

        assert recv(12) == b"RFB 003.008\n"
        ws.send_bytes(b"RFB 003.008\n")
        assert recv(2) == b"\x01\x01"  # security: None
        ws.send_bytes(b"\x01")
        assert recv(4) == b"\x00\x00\x00\x00"
        ws.send_bytes(b"\x01")
        width, height = struct.unpack(">HH", recv(4))
        recv(16)
        (name_len,) = struct.unpack(">I", recv(4))
        assert (width, height, recv(name_len)) == (800, 480, b"web")
        ws.send_bytes(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))
        assert recv(4) == b"\x00\x00\x00\x01"  # one rectangle
        assert struct.unpack(">HHHHi", recv(12)) == (0, 0, 800, 480, 0)
        assert len(recv(width * height * 4)) == width * height * 4
