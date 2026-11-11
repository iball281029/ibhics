"""Manager orchestration of migration certificates on fake nodes."""

from types import SimpleNamespace as NS

import pytest

from ibhics.cluster.migration_pki import PKIError
from ibhics.manager import migration_tls as mt


class FakePKI:
    def __init__(self, has_ca=True):
        self._ca, self.calls = has_ca, []

    def has_ca(self):
        return self._ca

    def ca_cert(self):
        return "CA"

    def sign(self, csr, name, address, purpose):
        self.calls.append(("sign", name, address, purpose))
        return f"cert:{name}:{purpose}"

    def prepare(self, name):
        self.calls.append(("prepare", name))
        return {"control": "csr", "migration": "csr"}

    def install(self, name, address, ca, certs, peers):
        self.calls.append(("install", name, address, tuple(peers)))
        return {"backup": "/root/b"}

    def set_peers(self, peers):
        self.calls.append(("peers", tuple(peers)))
        return peers

    def status(self):
        return {"control": {"renew": False}}


class Remote:
    def __init__(self, name, address, log):
        self.name, self.log, self.address = name, log, address
        self.fail = None

    def request(self, method, path, json=None, timeout=None):
        self.log.append((self.name, method, path, json))
        if self.fail and path.endswith(self.fail):
            return NS(status_code=422, content=b"x", json=lambda: {"detail": "boom"}, text="")
        body = {"/prepare": {"control": "c", "migration": "m"}, "/install": {"backup": "b"}}
        return NS(status_code=200, content=b"{}", json=lambda: body.get(path[14:], {}))


def cluster(*remote_names):
    log = []
    local = NS(name="n1", role="compute", local=True, address=None, client=None)
    members = [local]
    for i, n in enumerate(remote_names, start=2):
        members.append(
            NS(
                name=n,
                role="compute",
                local=False,
                address=f"http://10.0.0.{i}:8007",
                client=Remote(n, f"10.0.0.{i}", log),
            )
        )
    members.append(
        NS(name="w", role="witness", local=False, address="http://10.0.0.9:8007", client=None)
    )
    return NS(members=lambda: members), members, log


def test_enroll_signs_with_recorded_address_and_updates_every_compute():
    c, members, log = cluster("n2", "n3")
    pki, activated = FakePKI(), []
    tls = mt.MigrationTLS(c, "10.0.0.1", pki=pki, activate=lambda: activated.append("n1"))
    result = tls.enroll("n3")
    assert result["peers"] == ["n1", "n2", "n3"]
    assert ("sign", "n3", "10.0.0.3", "control") in pki.calls
    install = next(e for e in log if e[2] == "/migration-tls/install")
    assert install[0] == "n3" and install[3]["certs"] == {
        "control": "cert:n3:control",
        "migration": "cert:n3:migration",
    }
    assert ("n2", "POST", "/migration-tls/peers", {"peers": ["n1", "n2", "n3"]}) in log
    assert ("peers", ("n1", "n2", "n3")) in pki.calls  # the local node too
    activations = [e[0] for e in log if e[2] == "/migration-tls/activate"]
    assert sorted(activations) == ["n2", "n3"] and activated == ["n1"]
    assert tls.status()["events"][-1]["ok"] is True


def test_enroll_local_node_uses_local_address():
    c, _, _ = cluster("n2")
    pki = FakePKI()
    mt.MigrationTLS(c, "10.0.0.1", pki=pki, activate=lambda: None).enroll("n1")
    assert ("install", "n1", "10.0.0.1", ("n1", "n2")) in pki.calls


def test_enroll_failure_is_recorded_and_nothing_activated():
    c, members, log = cluster("n2")
    members[1].client.fail = "/install"
    tls = mt.MigrationTLS(c, "10.0.0.1", pki=FakePKI(), activate=lambda: log.append("local"))
    with pytest.raises(PKIError, match="boom"):
        tls.enroll("n2")
    assert not any(e == "local" or e[2] == "/migration-tls/activate" for e in log)
    event = tls.status()["events"][-1]
    assert event["ok"] is False and "boom" in event["error"]


def test_revoke_leaves_only_remaining_computes():
    c, members, log = cluster("n2")
    pki = FakePKI()
    del members[1]  # n3 already removed from the cluster
    tls = mt.MigrationTLS(c, "10.0.0.1", pki=pki, activate=lambda: None)
    assert tls.revoke("n3") == ["n1"]
    assert ("peers", ("n1",)) in pki.calls


def test_disabled_without_ca():
    c, _, _ = cluster("n2")
    tls = mt.MigrationTLS(c, "10.0.0.1", pki=FakePKI(has_ca=False), activate=lambda: None)
    assert not tls.enabled
    with pytest.raises(PKIError, match="no migration CA"):
        tls.enroll("n2")


def test_agent_exposes_certificate_steps():
    from fastapi.testclient import TestClient

    from ibhics.agent.app import create_agent_app
    from ibhics.cluster import NodeRole
    from ibhics.hypervisor.fake import FakeDriver

    pki = FakePKI()
    app = create_agent_app("n2", NodeRole.COMPUTE, "t", FakeDriver(), migration_pki=pki)
    client = TestClient(app)
    h = {"X-Ibhics-Token": "t"}
    assert client.post("/agent/v1/migration-tls/prepare", json={"name": "n2"}, headers=h).json()
    body = {
        "name": "n2",
        "address": "10.0.0.2",
        "ca": "CA",
        "certs": {"control": "a", "migration": "b"},
        "peers": ["n1", "n2"],
    }
    assert client.post("/agent/v1/migration-tls/install", json=body, headers=h).status_code == 200
    resp = client.post("/agent/v1/migration-tls/peers", json={"peers": "n1"}, headers=h)
    assert resp.status_code == 422
    assert client.get("/agent/v1/migration-tls").status_code == 401  # token required
    assert ("install", "n2", "10.0.0.2", ("n1", "n2")) in pki.calls
    plain = TestClient(create_agent_app("n2", NodeRole.COMPUTE, "t", FakeDriver()))
    assert plain.get("/agent/v1/migration-tls", headers=h).status_code == 404
