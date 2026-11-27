import json
import sys
import types

import pytest

libvirt = types.SimpleNamespace(
    VIR_DOMAIN_RUNNING=1, VIR_DOMAIN_PAUSED=3, VIR_DOMAIN_PAUSED_IOERROR=5, libvirtError=Exception
)


@pytest.fixture(autouse=True)
def fake_libvirt(monkeypatch):
    monkeypatch.setitem(sys.modules, "libvirt", libvirt)


from ibhics.hypervisor.io_recovery import IORecovery  # noqa: E402

DISK = {
    "driver": "file",
    "filename": "/mnt/ibhics/virtualstorage/vm-disk0.qcow2",
    "node-name": "libvirt-2-storage",
    "cache": {"direct": True, "no-flush": False},
}


class Dom:
    def __init__(self, state=(3, 5), resume_to=1):
        self._state, self.resume_to, self.resumed = state, resume_to, 0

    def name(self):
        return "vm"

    def UUIDString(self):
        return "u-1"

    def state(self):
        return self._state

    def resume(self):
        self.resumed += 1
        self._state = (self.resume_to, 0)


def make(probe=True, disks=(DISK,), reply=None):
    calls = []

    def monitor(dom, command):
        calls.append(command)
        return reply or {"return": {}}

    rec = IORecovery(
        lambda: None,
        probe=lambda d: probe,
        options_of=lambda pid: [dict(d) for d in disks],
        monitor=monitor,
        agent=lambda dom: True,
        sleep=lambda s: None,
    )
    return rec, calls


def test_reopens_with_flipped_then_original_options_and_resumes():
    rec, calls = make()
    dom = Dom()
    out = rec.recover(dom, 1)
    assert out["recovered"] and out["guest_agent"] and dom.resumed == 1
    sent = [c["arguments"]["options"][0]["cache"]["direct"] for c in calls]
    assert sent == [False, True]  # flipped once, then the original options back
    assert calls[1]["arguments"]["options"][0] == DISK


def test_waits_while_storage_probe_fails():
    rec, calls = make(probe=False)
    dom = Dom()
    out = rec.recover(dom, 1)
    assert not out["recovered"] and out["reason"] == "storage not ready yet"
    assert calls == [] and dom.resumed == 0


def test_refuses_disks_outside_managed_storage():
    rec, calls = make(disks=({**DISK, "filename": "/var/lib/libvirt/images/x.qcow2"},))
    out = rec.recover(Dom(), 1)
    assert not out["recovered"] and "outside" in out["reason"] and calls == []


def test_stops_when_qemu_refuses_reopen():
    rec, _ = make(reply={"error": {"class": "GenericError"}})
    dom = Dom()
    out = rec.recover(dom, 1)
    assert not out["recovered"] and dom.resumed == 0


def test_reports_when_guest_pauses_again():
    rec, _ = make()
    out = rec.recover(Dom(resume_to=3), 1)
    assert not out["recovered"] and out["reason"] == "paused again after resume"


def test_attempt_budget_per_hour():
    rec, _ = make()
    rec.max_attempts = 2
    for _ in range(2):
        rec.recover(Dom(), 1)
    out = rec.recover(Dom(), 1)
    assert out["reason"] == "recovery attempts exhausted for this hour"


def test_probe_directory_real(tmp_path):
    from ibhics.hypervisor.io_recovery import probe_directory

    assert probe_directory(str(tmp_path))
    assert not probe_directory(str(tmp_path / "missing"))
    assert json.dumps(DISK)
