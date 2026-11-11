"""One-shot LINSTOR/Gateway integration exercise inside the isolated controller VM."""

import socket
import subprocess

assert socket.gethostname() == "vs-test1", "Run only in the disposable controller VM"


def run(*args):
    print("Running:", " ".join(args), flush=True)
    subprocess.run(args, check=True, timeout=180)


for i in (1, 2, 3):
    run("linstor", "node", "create", f"vs-test{i}", f"192.0.2.1{i}")
for i in (1, 2):
    run(
        "linstor",
        "storage-pool",
        "create",
        "lvmthin",
        f"vs-test{i}",
        "ibhv-vs-test",
        "ibhv-vs-test/thin",
    )
run(
    "linstor",
    "resource-group",
    "create",
    "ibhv-vs-test",
    "--place-count",
    "2",
    "--storage-pool",
    "ibhv-vs-test",
    "--diskless-on-remaining",
    "true",
    "--layer-list",
    "DRBD,STORAGE",
)
for key, value in (
    ("DrbdOptions/auto-add-quorum-tiebreaker", "true"),
    ("DrbdOptions/auto-quorum", "suspend-io"),
):
    run("linstor", "resource-group", "set-property", "ibhv-vs-test", key, value)
run(
    "linstor-gateway",
    "nfs",
    "create",
    "vs-test",
    "192.0.2.16/24",
    "1G",
    "--resource-group",
    "ibhv-vs-test",
    "--allowed-ips",
    "192.0.2.0/24",
    "--filesystem",
    "xfs",
    "--resource-timeout",
    "120s",
)
run("linstor", "resource", "list")
run("linstor-gateway", "nfs", "list")
