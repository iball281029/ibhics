"""Operator-driven creation of two-replica Virtual Storage with a diskless Arbiter."""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .cluster.topology import NodeRole
from .hypervisor import PoolSpec, PoolType
from .hypervisor.storage_inventory import inventory
from .storage_cache import CacheModeIn, cache_status, set_cache_mode
from .storage_create import RF2_CREATION_POLICY, StorageCreateSpec, review_disks
from .storage_jobs import StorageJobs
from .storage_node import NodeProvisionIn, provision
from .storage_software import prepare_software, software_status


def remote(member, method: str, path: str, **kwargs):
    response = member.client.request(method, path, timeout=20, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{member.name}: {response.text[:1000]}")
    return response.json()


def inventories(cluster) -> dict:
    result = {}
    for member in cluster.members():
        if member.role != NodeRole.COMPUTE:
            continue
        row = {"name": member.name, "display_name": member.display_name}
        try:
            if not cluster.is_healthy(member):
                raise ValueError("Node is offline")
            row.update(
                inventory(member.driver)
                if member.local
                else remote(member, "GET", "/virtual-storage/inventory")
            )
        except Exception as exc:
            row.update(disks=[], fingerprint=None, error=str(exc))
        result[member.name] = row
    return result


class StorageController:
    def __init__(self, cluster, root: Path):
        self.cluster = cluster
        self.root = root
        self.jobs = StorageJobs(root / "manager-jobs")

    def _members(self):
        self.cluster.require_quorum()
        members = self.cluster.members()
        compute = [m for m in members if m.role == NodeRole.COMPUTE]
        witness = [m for m in members if m.role == NodeRole.WITNESS]
        if len(compute) != 2 or len(witness) != 1:
            raise ValueError("Virtual Storage requires two data nodes and one Arbiter")
        if not all(self.cluster.is_healthy(m) for m in members):
            raise ValueError("Both data nodes and Arbiter must be online")
        if len([m for m in compute if m.local]) != 1:
            raise ValueError("Create Virtual Storage on the cluster's management node")
        return compute, witness[0]

    def _wait(self, member, job, report):
        until = time.monotonic() + 3000
        while time.monotonic() < until:
            status = remote(member, "GET", "/virtual-storage/job")
            if not status or status["id"] != job["id"]:
                raise RuntimeError(f"{member.name}: storage job changed; inspect this node")
            report(f"{member.name}: {status['step']}")
            if status["state"] == "done":
                return status["result"]
            if status["state"] in ("failed", "interrupted"):
                raise RuntimeError(f"{member.name}: {status['error']}")
            time.sleep(2)
        raise RuntimeError(f"{member.name}: timed out waiting for storage preparation")

    def prepare(self) -> dict:
        compute, witness = self._members()

        def work(report):
            for member in compute + [witness]:
                report(f"Preparing storage software on {member.name}")
                if member.local:
                    prepare_software("compute", True, self.root, report)
                else:
                    job = remote(member, "POST", "/virtual-storage/software")
                    self._wait(member, job, report)
            return {"software_ready": True}

        return self.jobs.start("software", {}, work)

    @staticmethod
    def _gateway():
        with httpx.Client(base_url="http://127.0.0.1:8337", timeout=15) as client:
            response = client.get("/api/v2/nfs")
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _linstor_list(kind):
        done = subprocess.run(
            ["linstor", "--machine-readable", kind, "list"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        return json.loads(done.stdout)[0]

    def _check(self, spec):
        compute, witness = self._members()
        if {n.node for n in spec.nodes} != {m.name for m in compute}:
            raise ValueError("Selected data nodes do not match this cluster")
        if spec.arbiter != witness.name:
            raise ValueError("Select this cluster's Arbiter witness")
        for member in compute + [witness]:
            status = (
                software_status("compute", True)
                if member.local
                else remote(member, "GET", "/virtual-storage/readiness")
            )
            if not status.get("software_ready"):
                raise ValueError(f"Prepare storage software on {member.name} first")
            if status.get("creation_policy") != RF2_CREATION_POLICY:
                raise ValueError(f"Update storage software on {member.name} for the RF2 policy")
        if self._gateway():
            raise ValueError(
                "This cluster already has an NFS storage resource; only one is supported"
            )
        disks = inventories(self.cluster)
        for node, inspected in disks.items():
            if spec.name in inspected.get("previous_operations", []):
                raise ValueError(
                    f"{node}: a prior disk operation exists; inspect it before retrying"
                )
        review = review_disks(spec, disks)
        network = ipaddress.IPv4Network(f"{spec.virtual_ip}/{spec.prefix}", strict=False)
        addresses = {}
        interface = None
        for member in compute:
            matches = [
                a
                for a in disks[member.name]["addresses"]
                if ipaddress.IPv4Address(a["address"]) in network and a["prefix"] == spec.prefix
            ]
            if len(matches) != 1:
                raise ValueError(f"{member.name}: VIP must match one node interface and its prefix")
            addresses[member.name] = matches[0]["address"]
            if member.local:
                interface = matches[0]["interface"]
            selection = next(n for n in review["nodes"] if n["node"] == member.name)
            for pool in member.driver.list_pools():
                if pool.name == spec.name and pool.name not in selection["reclaim_pools"]:
                    raise ValueError(
                        f"{member.name}: a different storage pool already uses this name"
                    )
        address = str(ipaddress.IPv4Address(urlparse(witness.address).hostname))
        addresses[witness.name] = address
        if ipaddress.IPv4Address(address) not in network:
            raise ValueError("Arbiter must be reachable in the storage VIP subnet")
        if len(set(addresses.values()) | {spec.virtual_ip}) != 4:
            raise ValueError("Data nodes, Arbiter and VIP must have different IP addresses")
        for registered in self._linstor_list("node"):
            name = registered["name"]
            if name not in addresses or addresses[name] not in {
                nic["address"] for nic in registered.get("net_interfaces", [])
            }:
                raise ValueError("LINSTOR contains an unrelated node; use a dedicated controller")
        group = "ibhv-" + spec.name
        if any(r["name"] == group for r in self._linstor_list("resource-group")):
            raise ValueError("A previous storage resource group exists; inspect it before retrying")
        if any(p["storage_pool_name"] == group for p in self._linstor_list("storage-pool")):
            raise ValueError("A previous storage pool exists; inspect it before retrying")
        duplicate = subprocess.run(
            ["arping", "-D", "-I", interface, "-c", "2", "-w", "3", spec.virtual_ip],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if duplicate.returncode:
            raise ValueError("The storage VIP is in use or its availability could not be checked")
        review["addresses"] = addresses
        return review

    def review(self, spec: StorageCreateSpec) -> dict:
        review = self._check(spec)
        directory = self.root / "reviews"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        token = secrets.token_hex(24)
        receipt = {"spec": spec.model_dump(), "review": review, "expires": time.time() + 600}
        path = directory / (token + ".json")
        with path.open("x") as stream:
            os.chmod(path, 0o600)
            json.dump(receipt, stream)
        return {"review": review, "token": token, "expires_in": 600}

    def _wait_export(self, name, report):
        # A successful POST acknowledges the resource definition before Reactor
        # has necessarily mounted filesystems and started NFS/the VIP.
        until = time.monotonic() + 300
        while time.monotonic() < until:
            resource = next((r for r in self._gateway() if r["name"] == name), None)
            state = (resource or {}).get("status", {})
            if state.get("service") == "Started" and state.get("primary"):
                return resource
            report("Waiting for replicas, filesystem and shared storage VIP to start")
            time.sleep(2)
        raise RuntimeError("Shared storage did not start; inspect replication and service status")

    def create(self, token: str, confirm_name: str, confirm_wipe: bool) -> dict:
        import re

        if not re.fullmatch(r"[0-9a-f]{48}", token):
            raise ValueError("Review the selected disks before creating storage")
        path = self.root / "reviews" / (token + ".json")
        if not path.exists():
            raise ValueError("This creation review is no longer available")
        receipt = json.loads(path.read_text())
        spec = StorageCreateSpec(**receipt["spec"])
        if not confirm_wipe or confirm_name != spec.name:
            raise ValueError("Confirm disk erasure and type the exact storage name")
        if receipt["expires"] < time.time():
            raise ValueError("Creation review expired; refresh and review again")
        if self._check(spec) != receipt["review"]:
            raise ValueError("Storage configuration changed; review the disks again")

        def work(report):
            if self._check(spec) != receipt["review"]:
                raise ValueError("Storage changed before the operation started; review again")
            path.unlink()
            return self._create(spec, receipt["review"], report)

        return self.jobs.start("create", receipt["review"], work)

    def _create(self, spec, review, report):
        compute, witness = self._members()
        for selection in spec.nodes:
            member = self.cluster.member(selection.node)
            body = NodeProvisionIn(
                name=spec.name, selection=selection, size_gib=spec.size_gib, confirm_wipe=True
            )
            report(f"Preparing confirmed disks on {member.name}")
            if member.local:
                provision(member.driver, body, node=member.name, root=self.root, report=report)
            else:
                job = remote(member, "POST", "/virtual-storage/provision", json=body.model_dump())
                self._wait(member, job, report)

        return self._connect(spec, review, report)

    def _connect(self, spec, review, report):
        """Connect prepared providers. Never prepares or erases physical disks."""
        compute, witness = self._members()

        def command(*args, timeout=180):
            done = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout, check=False
            )
            if done.returncode:
                raise RuntimeError((done.stderr or done.stdout)[-2000:])
            return done.stdout

        report("Connecting data replicas and Arbiter")
        # A dedicated controller never adopts or changes an unrelated LINSTOR node.
        existing = json.loads(command("linstor", "--machine-readable", "node", "list"))[0]
        for member in compute + [witness]:
            match = next((n for n in existing if n["name"] == member.name), None)
            address = review["addresses"][member.name]
            if match:
                if address not in {n["address"] for n in match.get("net_interfaces", [])}:
                    raise ValueError(f"LINSTOR node {member.name} uses a different address")
            else:
                command("linstor", "node", "create", member.name, address)
        group = "ibhv-" + spec.name
        for member in compute:
            command(
                "linstor", "storage-pool", "create", "lvmthin", member.name, group, group + "/thin"
            )
        command(
            "linstor",
            "resource-group",
            "create",
            group,
            "--place-count",
            "2",
            "--peer-slots",
            "2",
            "--storage-pool",
            group,
            "--diskless-on-remaining",
            "true",
            "--layer-list",
            "DRBD,STORAGE",
        )
        for key, value in (
            ("DrbdOptions/auto-add-quorum-tiebreaker", "true"),
            ("DrbdOptions/auto-quorum", "suspend-io"),
        ):
            command("linstor", "resource-group", "set-property", group, key, value)
        report("Creating the shared export and synchronizing replicas")
        config = {
            "name": spec.name,
            "service_ip": f"{spec.virtual_ip}/{spec.prefix}",
            "resource_group": group,
            "implementation": "kernel",
            "allowed_ips": [review["addresses"][m.name] + "/32" for m in compute],
            "resource_timeout": 120_000_000_000,
            "volumes": [
                {
                    "number": 1,
                    "size_kib": spec.size_gib * 1024**2,
                    "file_system": "xfs",
                    "export_path": "/",
                    "file_system_root_owner": {"User": "root", "Group": "root"},
                }
            ],
        }
        with httpx.Client(base_url="http://127.0.0.1:8337", timeout=180) as client:
            response = client.post("/api/v2/nfs", json=config)
            if response.status_code != 201:
                raise RuntimeError("Shared export creation failed: " + response.text[-2000:])
        self._wait_export(spec.name, report)
        report("Mounting shared storage on both data nodes")
        export = "/srv/gateway-exports/" + spec.name
        for member in compute:
            if member.local:
                member.driver.create_pool(
                    PoolSpec(name=spec.name, type=PoolType.NFS, host=spec.virtual_ip, export=export)
                )
            else:
                response = member.client.request(
                    "POST",
                    "/pools",
                    timeout=180,
                    json={
                        "name": spec.name,
                        "type": "nfs",
                        "host": spec.virtual_ip,
                        "export": export,
                    },
                )
                response.raise_for_status()
            pool = next(p for p in member.driver.list_pools() if p.name == spec.name)
            if not pool.active or pool.capacity_gb <= 0:
                raise RuntimeError(f"{member.name}: shared pool did not mount successfully")
        resource = next(r for r in self._gateway() if r["name"] == spec.name)
        if resource["status"].get("service") != "Started":
            raise RuntimeError("Storage export is not running; inspect service status")
        result = {"spec": spec.model_dump(), "addresses": review["addresses"], "export": export}
        temporary = self.root / "created.tmp"
        temporary.write_text(json.dumps(result))
        temporary.chmod(0o600)
        temporary.replace(self.root / "created.json")
        return result

    def status(self) -> dict:
        path = self.root / "created.json"
        created = json.loads(path.read_text()) if path.exists() else None
        resource = None
        error = None
        if created:
            try:
                resource = next(
                    (r for r in self._gateway() if r["name"] == created["spec"]["name"]), None
                )
            except Exception as exc:
                error = str(exc)
        return {
            "created": created,
            "resource": resource,
            "error": error,
            "job": self.jobs.current(),
        }

    def cache(self):
        created = self.status()["created"]
        if not created:
            return []
        name = created["spec"]["name"]
        rows = []
        for selection in created["spec"]["nodes"]:
            if not selection["cache"]:
                continue
            member = self.cluster.member(selection["node"])
            try:
                info = (
                    cache_status(name)
                    if member.local
                    else remote(member, "GET", "/virtual-storage/cache/" + name)
                )
                rows.append({"node": member.name, **info})
            except Exception as exc:
                rows.append({"node": member.name, "error": str(exc)})
        return rows

    def change_cache(self, body: CacheModeIn):
        def check():
            self._members()
            status = self.status()
            if not status["created"] or status["created"]["spec"]["name"] != body.name:
                raise ValueError("Select the created Virtual Storage")
            if (status["resource"] or {}).get("status", {}).get("state") != "OK":
                raise ValueError("Data replicas and Arbiter must be healthy before changing cache")
            rows = self.cache()
            if not rows or any(r.get("error") for r in rows):
                raise ValueError("SSD cache status is unavailable; inspect both nodes")
            return rows

        check()

        def work(report):
            rows = check()
            for row in rows:
                member = self.cluster.member(row["node"])
                report("Changing SSD cache on " + member.name)
                if member.local:
                    set_cache_mode(body, root=self.root, report=report)
                else:
                    job = remote(member, "POST", "/virtual-storage/cache", json=body.model_dump())
                    self._wait(member, job, report)
            result = self.cache()
            if any(r.get("mode") != body.mode for r in result):
                raise RuntimeError("Cache modes differ; inspect both nodes before retrying")
            return {"mode": body.mode, "nodes": result}

        return self.jobs.start("cache-mode", body.model_dump(), work)
