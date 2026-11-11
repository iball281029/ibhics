"""Enroll, renew and revoke compute nodes' live-migration certificates.

The manager node holds the migration CA. Each node makes its keys itself and sends
only certificate requests; the manager signs them with the names and address the
cluster has on record and sends back public certificates.
"""

from __future__ import annotations

import threading
from datetime import datetime
from urllib.parse import urlparse

from ..cluster import Cluster, Member, NodeRole
from ..cluster.migration_pki import MigrationPKI, PKIError, schedule_activate


class _LocalNode:
    def __init__(self, pki: MigrationPKI, activate) -> None:
        self.pki, self._activate = pki, activate

    def prepare(self, name):
        return self.pki.prepare(name)

    def install(self, name, address, ca, certs, peers):
        return self.pki.install(name, address, ca, certs, peers)

    def set_peers(self, peers):
        return self.pki.set_peers(peers)

    def activate(self):
        self._activate()

    def status(self):
        return self.pki.status()


class _RemoteNode:
    def __init__(self, member: Member) -> None:
        self._client = member.client

    def _call(self, method, path, body=None):
        resp = self._client.request(method, f"/migration-tls{path}", json=body, timeout=120)
        if resp.status_code == 404:
            raise PKIError(f"{self._client.address} has no certificate support; update it")
        if resp.status_code >= 400:
            detail = resp.json().get("detail", resp.text) if resp.content else resp.status_code
            raise PKIError(f"{self._client.address}: {detail}")
        return resp.json()

    def prepare(self, name):
        return self._call("POST", "/prepare", {"name": name})

    def install(self, name, address, ca, certs, peers):
        body = {"name": name, "address": address, "ca": ca, "certs": certs, "peers": peers}
        return self._call("POST", "/install", body)

    def set_peers(self, peers):
        return self._call("POST", "/peers", {"peers": peers})

    def activate(self):
        self._call("POST", "/activate")

    def status(self):
        return self._call("GET", "")


class MigrationTLS:
    def __init__(
        self,
        cluster: Cluster,
        local_address: str,
        pki: MigrationPKI | None = None,
        activate=schedule_activate,
    ) -> None:
        self._cluster = cluster
        self._local_address = local_address
        self._pki = pki or MigrationPKI()
        self._local = _LocalNode(self._pki, activate)
        self._lock = threading.Lock()
        self.events: list[dict] = []  # last results, newest last; nothing secret

    @property
    def enabled(self) -> bool:
        """Only a cluster whose manager holds the CA enrolls nodes automatically."""
        return self._pki.has_ca()

    def _computes(self) -> list[Member]:
        return [m for m in self._cluster.members() if m.role == NodeRole.COMPUTE]

    def _node(self, member: Member):
        return self._local if member.local else _RemoteNode(member)

    def _address(self, member: Member) -> str:
        return self._local_address if member.local else urlparse(member.address).hostname or ""

    def _record(self, action: str, node: str, error: str | None = None) -> None:
        self.events.append(
            {
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "action": action,
                "node": node,
                "ok": error is None,
                "error": error,
            }
        )
        del self.events[:-50]

    def _activate_all(self, members: list[Member]) -> None:
        # Remote nodes first: the local activation restarts this manager.
        for member in sorted(members, key=lambda m: m.local):
            self._node(member).activate()

    def enroll(self, name: str, activate: bool = True) -> dict:
        """Issue certificates for one compute node and let every compute trust it."""
        with self._lock:
            try:
                result = self._enroll(name, activate)
            except Exception as exc:
                self._record("enroll", name, str(exc) or type(exc).__name__)
                raise
            self._record("enroll", name)
            return result

    def _enroll(self, name: str, activate: bool) -> dict:
        if not self.enabled:
            raise PKIError("the manager holds no migration CA; set up migration TLS first")
        member = next((m for m in self._computes() if m.name == name), None)
        if member is None:
            raise PKIError(f"no compute node named {name!r}")
        address = self._address(member)
        node = self._node(member)
        csrs = node.prepare(name)
        certs = {p: self._pki.sign(csr, name, address, p) for p, csr in csrs.items()}
        peers = sorted(m.name for m in self._computes())
        result = node.install(name, address, self._pki.ca_cert(), certs, peers)
        others = [m for m in self._computes() if m.name != name]
        for other in others:
            self._node(other).set_peers(peers)
        if activate:
            self._activate_all([member, *others])
        return {"node": name, "peers": peers, "backup": result.get("backup")}

    def renew_all(self) -> list[dict]:
        """New certificates for every compute node (same keys), then one restart each."""
        with self._lock:
            members, results = self._computes(), []
            for member in members:
                try:
                    results.append(self._enroll(member.name, activate=False))
                except Exception as exc:
                    self._record("renew", member.name, str(exc) or type(exc).__name__)
                    raise
                self._record("renew", member.name)
            self._activate_all(members)
            return results

    def revoke(self, name: str) -> list[str]:
        """After a node leaves: only the remaining computes may connect to libvirtd."""
        with self._lock:
            remaining = [m for m in self._computes() if m.name != name]
            peers = sorted(m.name for m in remaining)
            try:
                for member in remaining:
                    self._node(member).set_peers(peers)
                self._activate_all(remaining)
            except Exception as exc:
                self._record("revoke", name, str(exc) or type(exc).__name__)
                raise
            self._record("revoke", name)
            return peers

    def status(self) -> dict:
        nodes = {}
        for member in self._computes():
            try:
                nodes[member.name] = self._node(member).status()
            except Exception as exc:
                nodes[member.name] = {"error": str(exc) or type(exc).__name__}
        return {"enabled": self.enabled, "nodes": nodes, "events": list(self.events)}
