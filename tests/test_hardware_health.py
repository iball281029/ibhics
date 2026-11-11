import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from ibhics.agent import create_agent_app
from ibhics.cluster import AgentClient, NodeRole
from ibhics.cluster.remote import RemoteDriver
from ibhics.hypervisor import hardware
from ibhics.hypervisor.fake import FakeDriver
from ibhics.hypervisor.host import LinuxHost
from ibhics.manager import create_app


def test_sensors_distinguish_empty_slots_thresholds_and_discrete_faults():
    rows = hardware.sensors("""CPU1 Temp | 01h | ok | 3.1 | 38 degrees C
P2-DIMMF1 Temp | D4h | ns | 32.84 | No Reading
FAN1 | 41h | cr | 29.1 | 0 RPM
PS1 Status | C8h | ok | 10.1 | Presence detected, Failure detected
12V | 30h | nc | 7.17 | 10 Volts
garbage
""")
    assert [r["status"] for r in rows] == ["ok", "unknown", "critical", "critical", "warning"]
    assert rows[0]["category"] == "Temperature"
    assert rows[1]["category"] == "Temperature"  # not proof of memory health
    assert rows[3]["category"] == "Power supplies"


def test_memory_reports_ecc_and_does_not_claim_missing_telemetry_is_healthy(tmp_path):
    assert hardware.memory(tmp_path)[0]["status"] == "unknown"
    p = tmp_path / "sys/devices/system/edac/mc/mc1"
    p.mkdir(parents=True)
    (p / "ce_count").write_text("2")
    (p / "ue_count").write_text("1")
    d = p / "dimm0"
    d.mkdir()
    for key, value in {
        "size": "16384",
        "dimm_label": "P2-DIMMF1",
        "dimm_ce_count": "0",
        "dimm_ue_count": "1",
    }.items():
        (d / key).write_text(value)
    rows = hardware.memory(tmp_path)
    assert all(r["status"] == "critical" for r in rows)
    assert rows[1]["name"] == "P2-DIMMF1"


@pytest.mark.parametrize(
    "code,body,status",
    [
        (0, {"smart_status": {"passed": True}}, "ok"),
        (8, {"smart_status": {"passed": False}}, "critical"),
        (64, {"smart_status": {"passed": True}}, "warning"),
        (2, {"smart_status": {"passed": True}}, "unknown"),
        (127, {}, "unknown"),
        (124, {}, "unknown"),
    ],
)
def test_smart_exit_bitmask_and_missing_tools(code, body, status):
    row = hardware.disk_observation({"path": "/dev/sda"}, lambda c: (code, json.dumps(body)))
    assert row["status"] == status


def test_collection_is_read_only_and_history_is_not_current_fault(tmp_path):
    commands = []

    def run(cmd):
        commands.append(cmd)
        if "sdr" in cmd:
            return 0, "FAN1 | 41h | ok | 29.1 | 5900 RPM"
        if "sel" in cmd:
            return 0, "12 | 06/29/26 | 22:00:00 | Memory | Uncorrectable ECC | Asserted"
        if cmd[0] == "lsblk":
            return 0, json.dumps(
                {
                    "blockdevices": [
                        {"path": "/dev/sda", "size": 1000, "type": "disk"},
                        {"path": "/dev/nbd0", "size": 0, "type": "disk"},
                        {"path": "/dev/loop0", "size": 1000, "type": "loop"},
                    ]
                }
            )
        return 0, json.dumps({"smart_status": {"passed": True}})

    result = hardware.collect(tmp_path, run)
    assert len(result["events"]) == 1
    assert not any(i["status"] == "critical" for i in result["items"])
    assert [c for c in commands if c[0] == "smartctl"] == [
        ["smartctl", "-H", "-A", "-i", "-j", "/dev/sda"]
    ]
    assert all(c[0] in ("lsblk", "smartctl", "ipmitool") for c in commands)


def test_monitor_returns_immediately_and_starts_only_one_query():
    entered, finish, complete = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def collect():
        calls.append(1)
        entered.set()
        finish.wait(2)
        return {"items": [{"status": "ok"}], "events": []}

    monitor = hardware.Monitor(collect)
    assert monitor.snapshot()["collecting"]
    assert entered.wait(1)
    assert monitor.snapshot()["collecting"]
    assert len(calls) == 1
    finish.set()

    # Acquire the monitor lock after its worker completes without timing guesses.
    def wait_worker():
        for t in threading.enumerate():
            if t.name.endswith("(_collect)"):
                t.join(2)
        complete.set()

    wait_worker()
    assert complete.is_set()
    assert monitor.snapshot()["items"] == [{"status": "ok"}]
    assert len(calls) == 1


def test_chroot_does_not_probe_real_hardware(tmp_path):
    assert LinuxHost(root=tmp_path).hardware_health()["items"] == []


def test_hardware_endpoint_manager_agent_and_old_agent():
    driver = FakeDriver()
    driver.host_system().hardware_health = lambda: {
        "items": [{"name": "RAM", "status": "critical"}]
    }
    app = create_app(driver)
    client = TestClient(app)
    name = client.get("/api/v1/cluster").json()["members"][0]["name"]
    assert client.get(f"/api/v1/nodes/{name}/hardware").json()["items"][0]["status"] == "critical"
    agent = TestClient(create_agent_app("n", NodeRole.COMPUTE, "secret", driver))
    assert agent.get("/agent/v1/host/hardware").status_code == 401
    remote = RemoteDriver(AgentClient("http://agent", "secret", http=agent))
    assert remote.host_system().hardware_health()["items"][0]["name"] == "RAM"
    old = httpx.Client(
        base_url="http://agent", transport=httpx.MockTransport(lambda req: httpx.Response(404))
    )
    remote = RemoteDriver(AgentClient("http://agent", "secret", http=old))
    assert "Upgrade" in remote.host_system().hardware_health()["note"]


def test_verified_post_issue_survives_runtime_sensor_health(tmp_path):
    path = tmp_path / "etc/ibhics/hardware-issues.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            [
                {
                    "category": "Memory",
                    "name": "P2-DIMMF1",
                    "status": "critical",
                    "detail": "Uncorrectable memory component found during BIOS POST",
                    "source": "Verified BIOS POST",
                    "observed_at": "2026-11-09T16:46:00+07:00",
                }
            ]
        )
    )
    issue = hardware.recorded_issues(tmp_path)[0]
    assert issue["status"] == "critical"
    assert issue["name"] == "P2-DIMMF1"
    assert "(recorded)" in issue["source"]
    assert "2026-11-09" in issue["detail"]
    path.unlink()
    assert hardware.recorded_issues(tmp_path) == []


def test_invalid_issue_file_is_unknown_not_a_clean_bill(tmp_path):
    path = tmp_path / "etc/ibhics/hardware-issues.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    assert hardware.recorded_issues(tmp_path)[0]["status"] == "unknown"
