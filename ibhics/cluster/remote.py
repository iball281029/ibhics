"""HTTP client for node agents, and a HypervisorDriver that runs through it."""

from __future__ import annotations

import httpx

from ..hypervisor import (
    DiskSpec,
    HostInfo,
    HypervisorDriver,
    InvalidState,
    NicSpec,
    VMExists,
    VMInfo,
    VMNotFound,
    VMSpec,
)

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

    def health(self) -> dict:
        resp = self.request("GET", "/health")
        if resp.status_code != 200:
            raise NodeUnavailable(f"{self.address}: health check returned {resp.status_code}")
        return resp.json()


def info_from_json(data: dict) -> VMInfo:
    spec = VMSpec(
        name=data["name"],
        vcpus=data["vcpus"],
        memory_mb=data["memory_mb"],
        firmware=data["firmware"],
        disks=[DiskSpec(**d) for d in data["disks"]],
        nics=[NicSpec(**n) for n in data["nics"]],
        iso=data.get("iso"),
        description=data.get("description", ""),
    )
    return VMInfo(uuid=data["uuid"], spec=spec, state=data["state"])


def spec_to_json(spec: VMSpec) -> dict:
    return {
        "name": spec.name,
        "vcpus": spec.vcpus,
        "memory_mb": spec.memory_mb,
        "firmware": str(spec.firmware),
        "disks": [{"size_gb": d.size_gb, "backend": d.backend, "pool": d.pool} for d in spec.disks],
        "nics": [{"network": n.network, "model": n.model, "mac": n.mac} for n in spec.nics],
        "iso": spec.iso,
        "description": spec.description,
    }


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

    def _ok(self, resp: httpx.Response, uuid: str = "") -> httpx.Response:
        if resp.status_code == 404:
            raise VMNotFound(uuid)
        if resp.status_code == 409:
            raise InvalidState(resp.json().get("detail", "invalid state"))
        if resp.status_code >= 400:
            raise NodeUnavailable(f"{self._client.address}: HTTP {resp.status_code}")
        return resp
