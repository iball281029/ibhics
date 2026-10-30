"""HTTP client for node agents, and a HypervisorDriver that runs through it."""

from __future__ import annotations

import httpx

from ..backup import BackupInfo, BackupNotFound, BackupTarget, TargetUnavailable
from ..hypervisor import (
    HostInfo,
    HypervisorDriver,
    InvalidState,
    PoolInfo,
    PoolNotFound,
    PoolSpec,
    VMExists,
    VMInfo,
    VMNotFound,
    VMSpec,
)
from ..hypervisor.serialize import info_from_json, spec_to_json

TOKEN_HEADER = "X-Ibhics-Token"


class NodeUnavailable(RuntimeError):
    pass


class AgentClient:
    def __init__(
        self,
        address: str,
        token: str,
        http: httpx.Client | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.address = address.rstrip("/")
        self._http = http or httpx.Client(base_url=self.address, timeout=timeout)
        self._headers = {TOKEN_HEADER: token}

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            resp = self._http.request(method, f"/agent/v1{path}", headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise NodeUnavailable(f"{self.address}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise NodeUnavailable(f"{self.address}: cluster token rejected")
        return resp

    def open_console(self, uuid: str):
        """Async context manager: a WebSocket to the agent's console relay for this VM."""
        from websockets.asyncio.client import connect

        scheme, rest = self.address.split("://", 1)
        url = f"{'wss' if scheme == 'https' else 'ws'}://{rest}/agent/v1/vms/{uuid}/console"
        return connect(url, additional_headers=self._headers, max_size=None, open_timeout=5)

    def health(self) -> dict:
        resp = self.request("GET", "/health")
        if resp.status_code != 200:
            raise NodeUnavailable(f"{self.address}: health check returned {resp.status_code}")
        return resp.json()


def _pool_info(data: dict) -> PoolInfo:
    data = {k: v for k, v in data.items() if k != "node"}
    return PoolInfo(**data)


class RemoteDriver(HypervisorDriver):
    def __init__(self, client: AgentClient) -> None:
        self._client = client

    def host_info(self) -> HostInfo:
        return HostInfo(**self._ok(self._client.request("GET", "/host")).json())

    def list_vms(self) -> list[VMInfo]:
        return [info_from_json(vm) for vm in self._ok(self._client.request("GET", "/vms")).json()]

    def get_vm(self, uuid: str) -> VMInfo:
        return info_from_json(self._ok(self._client.request("GET", f"/vms/{uuid}"), uuid).json())

    def create_vm(self, spec: VMSpec) -> VMInfo:
        resp = self._client.request("POST", "/vms", json=spec_to_json(spec))
        if resp.status_code == 409:
            raise VMExists(resp.json().get("detail", "VM already exists"))
        if resp.status_code == 422:
            raise ValueError(str(resp.json().get("detail")))
        if resp.status_code == 404:  # the only 404 on create is a missing pool
            detail = resp.json().get("detail", "")
            raise PoolNotFound(detail.removeprefix("storage pool not found: "))
        return info_from_json(self._ok(resp).json())

    def start_vm(self, uuid: str) -> VMInfo:
        return info_from_json(
            self._ok(self._client.request("POST", f"/vms/{uuid}/start"), uuid).json()
        )

    def stop_vm(self, uuid: str, force: bool = False) -> VMInfo:
        resp = self._client.request("POST", f"/vms/{uuid}/stop", json={"force": force})
        return info_from_json(self._ok(resp, uuid).json())

    def delete_vm(self, uuid: str) -> None:
        self._ok(self._client.request("DELETE", f"/vms/{uuid}"), uuid)

    def list_pools(self) -> list[PoolInfo]:
        return [_pool_info(p) for p in self._ok(self._client.request("GET", "/pools")).json()]

    def create_pool(self, spec: PoolSpec) -> PoolInfo:
        body = {k: str(v) if k == "type" else v for k, v in spec.__dict__.items()}
        resp = self._client.request("POST", "/pools", json=body)
        if resp.status_code == 422:
            raise ValueError(str(resp.json().get("detail")))
        return _pool_info(self._ok(resp).json())

    def delete_pool(self, name: str) -> None:
        resp = self._client.request("DELETE", f"/pools/{name}")
        if resp.status_code == 404:
            raise PoolNotFound(name)
        self._ok(resp)

    def _ok(self, resp: httpx.Response, uuid: str = "") -> httpx.Response:
        if resp.status_code == 404:
            raise VMNotFound(uuid)
        if resp.status_code == 409:
            raise InvalidState(resp.json().get("detail", "invalid state"))
        if resp.status_code >= 400:
            raise NodeUnavailable(f"{self._client.address}: HTTP {resp.status_code}")
        return resp


def _backup_info(data: dict) -> BackupInfo:
    return BackupInfo(**{k: v for k, v in data.items() if k != "node"})


class RemoteBackup:
    """NodeBackup on another node, through its agent. Jobs can take hours, so no timeout."""

    def __init__(self, client: AgentClient) -> None:
        self._client = client

    def backup(self, uuid: str, target: BackupTarget) -> BackupInfo:
        body = {"uuid": uuid, "target": target.to_dict()}
        return _backup_info(self._call("/backups/run", body, uuid).json())

    def list(self, target: BackupTarget, vm_name: str | None = None) -> list[BackupInfo]:
        body = {"target": target.to_dict(), "vm_name": vm_name}
        return [_backup_info(b) for b in self._call("/backups/list", body).json()]

    def restore(
        self,
        target: BackupTarget,
        backup_id: str,
        name: str | None = None,
        pool: str | None = None,
    ) -> VMInfo:
        body = {"target": target.to_dict(), "backup_id": backup_id, "name": name, "pool": pool}
        return info_from_json(self._call("/backups/restore", body).json())

    def delete(self, target: BackupTarget, backup_id: str) -> int:
        body = {"target": target.to_dict(), "backup_id": backup_id}
        return self._call("/backups/delete", body).json()["freed_chunks"]

    def prune(self, target: BackupTarget, vm_name: str, keep_last: int) -> list[str]:
        body = {"target": target.to_dict(), "vm_name": vm_name, "keep_last": keep_last}
        return self._call("/backups/prune", body).json()["removed"]

    def _call(self, path: str, body: dict, uuid: str = "") -> httpx.Response:
        resp = self._client.request("POST", path, json=body, timeout=None)
        if resp.status_code < 400:
            return resp
        detail = str(resp.json().get("detail", "")) if resp.content else ""
        if resp.status_code == 404:
            if detail.startswith("backup not found: "):
                raise BackupNotFound(detail.removeprefix("backup not found: "))
            if detail.startswith("storage pool not found: "):
                raise PoolNotFound(detail.removeprefix("storage pool not found: "))
            raise VMNotFound(uuid)
        if resp.status_code == 409:
            raise InvalidState(detail)
        if resp.status_code == 422:
            raise ValueError(detail)
        if resp.status_code == 502:
            raise TargetUnavailable(detail.removeprefix("backup target unavailable: "))
        raise NodeUnavailable(f"{self._client.address}: HTTP {resp.status_code} {detail}")
