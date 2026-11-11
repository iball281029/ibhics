"""Read-only local BMC discovery and compatible node summaries."""

import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ibhics.agent import create_agent_app
from ibhics.cluster import AgentClient, NodeRole
from ibhics.cluster.remote import RemoteDriver
from ibhics.hypervisor import host
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app

LAN = """Set in Progress : Set Complete
IP Address Source : Static Address
IP Address : 172.16.12.108
Subnet Mask : 255.255.255.0
MAC Address : AC:1F:6B:6F:69:5D
Default Gateway IP : 172.16.12.254
SNMP Community String : do-not-return-this
"""


def test_reads_lan_without_changing_bmc_or_returning_secrets():
    commands = []

    def run(cmd):
        commands.append(cmd)
        return 0, LAN

    assert host.ipmi_info(run) == {
        "status": "configured",
        "ip_address": "172.16.12.108",
        "netmask": "255.255.255.0",
        "gateway": "172.16.12.254",
        "mac_address": "ac:1f:6b:6f:69:5d",
        "source": "Static Address",
    }
    assert commands == [["ipmitool", "-I", "open", "lan", "print"]]


@pytest.mark.parametrize("ip", ["0.0.0.0", "garbage", "127.0.0.1", "224.0.0.1", ""])
def test_invalid_or_unconfigured_ip_has_no_console_link(ip):
    data = host.ipmi_info(lambda cmd: (0, f"IP Address : {ip}"))
    assert data["status"] == "unconfigured"
    assert "ip_address" not in data


@pytest.mark.parametrize("code", [127, 124, 126, 1])
def test_missing_tool_or_bmc_is_reported_without_breaking_summary(code):
    result = host.ipmi_info(lambda cmd: (code, ""))
    assert result["status"] == "unavailable"
    assert result["note"]


def test_ipmi_query_is_bounded_and_read_only(monkeypatch):
    def run(cmd, **kwargs):
        assert cmd == ["ipmitool", "-I", "open", "lan", "print"]
        assert kwargs["timeout"] == 4
        assert kwargs.get("shell", False) is False
        raise subprocess.TimeoutExpired(cmd, 4)

    monkeypatch.setattr(host.subprocess, "run", run)
    assert host._read_ipmi(["ipmitool", "-I", "open", "lan", "print"]) == (124, "")


def test_query_cache_and_chroot_roots(monkeypatch):
    clock = [0]
    calls = []
    monkeypatch.setattr(host.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(host, "ipmi_info", lambda: calls.append(1) or {"ip_address": "10.0.0.1"})
    node = host.LinuxHost()
    assert node._ipmi() == {"ip_address": "10.0.0.1"}
    node._ipmi()
    assert len(calls) == 1
    clock[0] = 301
    node._ipmi()
    assert len(calls) == 2
    assert host.LinuxHost(root=Path("/fake-root"))._ipmi() == {}
    assert len(calls) == 2


def test_api_and_remote_agent_include_bmc_and_accept_old_summaries(monkeypatch):
    driver = FakeDriver()
    summary = driver.host_system().summary()
    summary.ipmi = host.ipmi_info(lambda cmd: (0, LAN))
    monkeypatch.setattr(driver.host_system(), "summary", lambda: summary)
    client = TestClient(create_app(driver))
    node = client.get("/api/v1/cluster").json()["members"][0]["name"]
    assert (
        client.get(f"/api/v1/nodes/{node}/summary").json()["ipmi"]["ip_address"] == "172.16.12.108"
    )
    agent = TestClient(create_agent_app("node2", NodeRole.COMPUTE, "test-token", driver))
    remote = RemoteDriver(AgentClient("http://node2", "test-token", http=agent)).host_system()
    assert remote.summary().ipmi == summary.ipmi
    legacy = asdict(summary)
    legacy.pop("ipmi")
    assert host.NodeSummary(**legacy).ipmi == {}
