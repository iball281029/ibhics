"""Copy the development helpers into the isolated lab and exercise its virtual disks."""

import shlex
import subprocess

OPTIONS = ["-i", "/root/ibhics-vstorage-lab/key", "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
SOURCE = "/root/ibhics-vstorage-prepare/"


def ssh(node, args):
    return subprocess.run(["ssh", *OPTIONS, "root@" + node, shlex.join(args)],
                          check=True, capture_output=True, text=True).stdout.strip()


def copy(node, source, target):
    subprocess.run(["scp", *OPTIONS, SOURCE + source, "root@" + node + ":" + target],
                   check=True)


for number in (1, 2):
    node = f"192.0.2.1{number}"
    target = ssh(node, ["/opt/ibhics/venv/bin/python", "-c",
                       "import ibhics,os;print(os.path.dirname(ibhics.__file__))"])
    assert target.startswith("/opt/ibhics/venv/")
    for file in ("storage_create.py", "storage_node.py"):
        copy(node, file, target + "/" + file)
    copy(node, "storage_inventory.py", target + "/hypervisor/storage_inventory.py")
    copy(node, "exercise-test-disks.py", "/root/exercise-test-disks.py")
    print(ssh(node, ["/opt/ibhics/venv/bin/python", "/root/exercise-test-disks.py"]), flush=True)
