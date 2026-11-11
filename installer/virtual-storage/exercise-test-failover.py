"""Abruptly power off only the isolated primary QEMU guest and check real NFS I/O."""

import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

LAB = Path("/root/ibhics-vstorage-lab")
OPTIONS = [
    "-i",
    str(LAB / "key"),
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
]
client, fresh = LAB / "client", LAB / "fresh-client"
client.mkdir(exist_ok=True)
fresh.mkdir(exist_ok=True)
source = "192.0.2.16:/srv/gateway-exports/vs-test"
pid = int((LAB / "node1/qemu.pid").read_text())
args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().rstrip("\0").split("\0")
assert args[0] == "qemu-system-x86_64" and str(LAB / "node1/os.qcow2") in " ".join(args)
(LAB / "node1/restart-command.json").write_text(json.dumps(args))
powered_off = False
proof = {}
try:
    subprocess.run(
        ["mount", "-t", "nfs", "-o", "vers=4.2,hard,timeo=10,retrans=2", source, str(client)],
        check=True,
        timeout=30,
    )
    data = os.urandom(8 * 1024**2)
    with (client / "before-failover.bin").open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    expected = hashlib.sha256(data).hexdigest()
    print("Initial NFS write synced; abruptly stopping only lab node1", flush=True)
    os.kill(pid, signal.SIGKILL)
    powered_off = True
    began = time.monotonic()
    for _ in range(60):
        result = subprocess.run(
            ["ssh", *OPTIONS, "root@192.0.2.12", "drbdsetup status vs-test --json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and json.loads(result.stdout)[0]["role"] == "Primary":
            break
        time.sleep(2)
    else:
        raise RuntimeError("Standby did not become Primary")
    proof["promotion_seconds"] = round(time.monotonic() - began, 1)
    print("Standby became Primary; checking existing mount and a fresh mount", flush=True)
    subprocess.run(
        [
            "dd",
            "if=/dev/zero",
            f"of={client}/after-failover.bin",
            "bs=1M",
            "count=8",
            "conv=fsync",
            "status=none",
        ],
        check=True,
        timeout=180,
    )
    subprocess.run(
        ["mount", "-t", "nfs", "-o", "vers=4.2,hard,timeo=10,retrans=2", source, str(fresh)],
        check=True,
        timeout=30,
    )
    observed = hashlib.sha256((fresh / "before-failover.bin").read_bytes()).hexdigest()
    assert expected == observed, "Data changed after failover"
    assert (fresh / "after-failover.bin").stat().st_size == 8 * 1024**2
    proof.update(
        nfs_write_after_failover=True,
        preserved_sha256=observed,
        total_seconds=round(time.monotonic() - began, 1),
    )
    (LAB / "failover-proof.json").write_text(json.dumps(proof, indent=2))
    print(json.dumps(proof), flush=True)
finally:
    if powered_off:
        subprocess.run(args, check=True)
        print("Restarted isolated lab node1", flush=True)
    for mount in (fresh, client):
        try:
            subprocess.run(["umount", str(mount)], timeout=15, capture_output=True)
        except subprocess.TimeoutExpired:
            subprocess.run(["umount", "-l", str(mount)], check=False)
