"""Exercise actual LVM/cache commands only on the isolated lab's marked virtual disks."""

import json
import socket
from pathlib import Path

from ibhics.hypervisor.storage_inventory import inventory
from ibhics.storage_create import NodeDisks
from ibhics.storage_node import NodeProvisionIn, provision


class EmptyConnection:
    def listAllStoragePools(self):
        return []

    def listAllDomains(self):
        return []


class LabDriver:
    _conn = EmptyConnection()


node = socket.gethostname()
assert node in ("vs-test1", "vs-test2"), "This exercise must run in the disposable test guests"
driver = LabDriver()
current = inventory(driver)
data = [d["path"] for d in current["disks"] if d["serial"] == f"VS-TEST-DATA-{node[-1]}"]
cache = [d["path"] for d in current["disks"] if d["serial"] == f"VS-TEST-CACHE-{node[-1]}"]
assert len(data) == len(cache) == 1
# VirtIO defaults to rotational; emulate the marked cache disk as an SSD.
Path(f"/sys/class/block/{Path(cache[0]).name}/queue/rotational").write_text("0")
current = inventory(driver)
selection = NodeDisks(node=node, fingerprint=current["fingerprint"], data=data, cache=cache)
print(json.dumps(provision(driver, NodeProvisionIn(name="vs-test", selection=selection,
    size_gib=1, confirm_wipe=True), node=node, report=lambda s: print(s, flush=True))))
