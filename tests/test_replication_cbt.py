"""Offline CBT ranges, fallback and cleanup; no source network connections."""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from ibhics.importer import SourceType
from ibhics.replication.cbt import ESXiCBTProvider
from ibhics.replication.store import ReplicaStore
from ibhics.replication.vsphere import CBTUnavailable, VSphereCBT
from tests.test_replication_ssh import SOURCE, VM, Commands

SOURCE = replace(SOURCE, type=SourceType.VMWARE, username="root")
VM = replace(VM, ref="42", vmx="[datastore] vm/vm.vmx")


class CBT:
    def __init__(self, source, *, failure=None, change_id="epoch/2"):
        self.failure, self.change_id = failure, change_id
        self.calls = []
        self.closed = False

    def connect(self, source):
        if self.failure == "login":
            raise RuntimeError("password=TOP-SECRET")

    def enable(self, ref):
        self.calls.append(("enable", ref))

    def snapshot_disks(self, ref, name):
        assert name.startswith("ibhics-repl-")
        if self.failure == "snapshot":
            raise RuntimeError("changeId not ready")
        return "snapshot-900", {
            "[datastore] vm/disk.vmdk": {"key": 2000, "size": 8, "change_id": self.change_id}
        }

    def changed(self, *args):
        self.calls.append(args)
        if self.failure == "query":
            raise RuntimeError("InvalidArgument: changeId reset; TOP-SECRET")
        return [(5, 1)]

    def close(self):
        self.closed = True


def provider(commands=None, **kwargs):
    commands = commands or Commands("esxi")
    api = CBT(SOURCE, **kwargs)
    return ESXiCBTProvider(SOURCE, VM, commands, lambda source: api), commands, api


def test_baseline_then_cbt_reads_only_changed_chunk(tmp_path):
    # Replace dd here to allow tiny test chunks; production keeps 4 MiB alignment.
    class Small(ESXiCBTProvider):
        def hash(self, disk, offset, length):
            self.scans.append(offset)
            return None

        def read(self, disk, offset, length):
            return self.data[offset : offset + length]

    def cycle(data):
        commands, api = Commands("esxi"), CBT(SOURCE)
        instance = Small(SOURCE, VM, commands, lambda source: api)
        instance.data, instance.scans = data, []
        return instance, commands, api

    store = ReplicaStore(tmp_path / "metadata", [tmp_path / "pool"], chunk=4)
    first, _, _ = cycle(b"aaaabbbb")
    baseline = store.sync(first)
    assert baseline["change_tracking"] == "hash-delta"
    assert "baseline" in baseline["fallback_reason"]
    second, commands, api = cycle(b"aaaacccc")
    result = store.sync(second)
    assert second.scans == [4]
    assert result["change_tracking"] == "cbt" and result["fallback_reason"] is None
    assert api.calls[-1] == ("42", "snapshot-900", 2000, 8, "epoch/2")
    assert result["bytes_transferred"] == 4
    assert Path(result["disks"][0]["path"]).read_bytes() == b"aaaacccc"
    assert commands.snapshot is None and api.closed


@pytest.mark.parametrize("failure", ["login", "snapshot", "query", "bad-token"])
def test_fallback_is_full_hash_scan_and_secret_free(tmp_path, failure):
    store = ReplicaStore(tmp_path / "metadata", [tmp_path / "pool"])
    initial, _, _ = provider()
    before = store.sync(initial)
    if failure == "bad-token":
        manifest = tmp_path / "metadata" / "current.json"
        before["token"] = "old-phase-one-token"
        manifest.write_text(json.dumps(before))
    current, commands, api = provider(failure=failure)
    result = store.sync(current)
    assert result["change_tracking"] == "hash-delta"
    assert result["fallback_reason"]
    assert "TOP-SECRET" not in json.dumps(result)
    assert commands.snapshot is None and api.closed
    assert any(c.startswith("dd ") for c in commands.commands)


def test_read_failure_removes_owned_snapshot_and_keeps_manifest(tmp_path):
    store = ReplicaStore(tmp_path / "metadata", [tmp_path / "pool"])
    initial, _, _ = provider()
    before = store.sync(initial)

    class FailRead(Commands):
        def command(self, command, binary=False):
            if command.startswith("dd "):
                raise RuntimeError("read failed")
            return super().command(command, binary)

    current, commands, api = provider(FailRead("esxi"))
    with pytest.raises(RuntimeError, match="read failed"):
        store.sync(current)
    assert commands.snapshot is None and api.closed
    assert store.current() == before


def soap(body):
    return (
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        '<s:Body><Response xmlns="urn:vim25">' + body + "</Response></s:Body></s:Envelope>"
    )


def test_soap_query_paginates_and_escapes_change_id():
    requests = []

    def respond(request):
        requests.append(request.content.decode())
        offset = 0 if len(requests) == 1 else 4
        return httpx.Response(
            200,
            text=soap(
                f"<returnval><startOffset>{offset}</startOffset><length>4</length>"
                f"<changedArea><start>{offset + 1}</start><length>1</length></changedArea>"
                "</returnval>"
            ),
        )

    client = VSphereCBT(
        SOURCE, http=httpx.Client(base_url="https://esxi", transport=httpx.MockTransport(respond))
    )
    assert client.changed("42", "snapshot-900", 2000, 8, "epoch<&") == [(1, 1), (5, 1)]
    assert "epoch&lt;&amp;" in requests[0]
    assert "<startOffset>4</startOffset>" in requests[1]
    client.close()


@pytest.mark.parametrize(
    "body",
    [
        "<returnval><startOffset>0</startOffset><length>0</length></returnval>",
        "<returnval><startOffset>4</startOffset><length>4</length></returnval>",
        "<returnval><startOffset>0</startOffset><length>8</length>"
        "<changedArea><start>7</start><length>2</length></changedArea></returnval>",
        "<Fault><faultstring>TOP-SECRET</faultstring></Fault>",
    ],
)
def test_soap_invalid_coverage_or_fault_rejected_without_secrets(body):
    client = VSphereCBT(
        SOURCE,
        http=httpx.Client(
            base_url="https://esxi",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=soap(body))),
        ),
    )
    with pytest.raises(CBTUnavailable) as error:
        client.changed("42", "snapshot-900", 2000, 8, "epoch/1")
    assert "TOP-SECRET" not in str(error.value)
    client.close()


def test_named_vm_resolved_by_exact_vmx_before_snapshot():
    class Inventory(Commands):
        def command(self, command, binary=False):
            if command == "vim-cmd vmsvc/getallvms":
                return (
                    "Vmid Name File Guest OS Version\n"
                    "42 vm [datastore] vm/vm.vmx otherGuest vmx-19\n"
                    "43 vm [other] vm/vm.vmx otherGuest vmx-19\n"
                )
            return super().command(command, binary)

    api = CBT(SOURCE)
    instance = ESXiCBTProvider(
        SOURCE, replace(VM, ref="display-name"), Inventory("esxi"), lambda source: api
    )
    try:
        point = instance.begin()
        assert json.loads(point.token)["vm"] == "42"
    finally:
        instance.end()
    assert ("enable", "42") in api.calls


def test_soap_login_enable_and_snapshot_device_ids():
    import xml.etree.ElementTree as ET

    methods = []

    def respond(request):
        root = ET.fromstring(request.content)
        method = list(list(root)[0])[0]
        name = method.tag.rsplit("}", 1)[-1]
        methods.append(name)
        xml = request.content.decode()
        if name == "RetrieveServiceContent":
            body = (
                "<returnval><propertyCollector>pc</propertyCollector>"
                "<sessionManager>sm</sessionManager></returnval>"
            )
        elif name in ("Login", "Logout"):
            body = "<returnval/>"
            if name == "Login":
                assert "TOP-SECRET" in xml
        elif name == "ReconfigVM_Task":
            assert "<changeTrackingEnabled>true</changeTrackingEnabled>" in xml
            body = '<returnval type="Task">task-1</returnval>'
        elif "capability.changeTrackingSupported" in xml:
            body = (
                "<propSet><name>capability.changeTrackingSupported</name><val>true</val></propSet>"
            )
        elif "config.changeTrackingEnabled" in xml:
            body = "<propSet><name>config.changeTrackingEnabled</name><val>false</val></propSet>"
        elif "info.state" in xml:
            body = "<propSet><name>info.state</name><val>success</val></propSet>"
        elif "config.hardware.device" in xml:
            body = """<propSet><name>config.hardware.device</name>
            <val xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
              <VirtualDevice xsi:type="VirtualDisk"><key>2000</key>
                <capacityInKB>4</capacityInKB>
                <backing xsi:type="VirtualDiskFlatVer2BackingInfo">
                  <fileName>[datastore] vm/disk.vmdk</fileName><changeId>epoch/1</changeId>
                </backing>
              </VirtualDevice>
            </val></propSet>"""
        else:
            assert "<pathSet>snapshot</pathSet>" in xml
            body = """<propSet><name>snapshot</name><val>
            <currentSnapshot>snapshot-900</currentSnapshot><rootSnapshotList>
              <snapshot>snapshot-900</snapshot><name>ibhics-repl-test</name><id>9</id>
            </rootSnapshotList></val></propSet>"""
        return httpx.Response(200, text=soap(body))

    client = VSphereCBT(
        SOURCE, http=httpx.Client(base_url="https://esxi", transport=httpx.MockTransport(respond))
    )
    client.connect(SOURCE)
    client.enable("42")
    snapshot, disks = client.snapshot_disks("42", "ibhics-repl-test")
    assert snapshot == "snapshot-900"  # vim-cmd snapshot id 9 is not the SOAP MOR
    assert disks == {
        "[datastore] vm/disk.vmdk": {"key": 2000, "size": 4096, "change_id": "epoch/1"}
    }
    with pytest.raises(CBTUnavailable, match="ownership"):
        client.snapshot_disks("42", "ibhics-repl-someone-else")
    client.close()
    assert methods[0:2] == ["RetrieveServiceContent", "Login"]
    assert methods[-1] == "Logout"
