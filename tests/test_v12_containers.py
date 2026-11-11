"""1.2 containers: Docker that works unprivileged, Nginx Proxy Manager, memory use on
cgroup v2, the pool column and moving to another pool, long console commands."""

import json
import sys
import time

from fastapi.testclient import TestClient
from test_containers import wait_for

from ibhics.hypervisor.containers import (
    ContainerSpec,
    ExecJobs,
    LxcContainers,
    first_boot_script,
    lxc_config,
)
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager import create_app


def test_docker_apps_turn_nesting_on_with_fuse_and_overlay():
    for app in ("app:docker", "app:npm"):
        spec = ContainerSpec("d1", template=app)
        spec.validate()
        assert spec.nesting
        cfg = lxc_config(spec, "br0", "/p/rootfs.img")
        assert "lxc.apparmor.allow_nesting = 1" in cfg
        assert "mount fstype=overlay," in cfg and "dev/fuse" in cfg
        assert "lxc.cgroup2.devices.allow = c 10:229 rwm" in cfg
        assert "lxc.cgroup2.devices.allow = c 1:3 rwm" in cfg  # the defaults stay
        script = first_boot_script(spec)
        assert "fuse-overlayfs" in script and "storage-driver" in script
    npm = first_boot_script(ContainerSpec("p1", template="app:npm"))
    assert "jc21/nginx-proxy-manager" in npm and "docker-compose up -d" in npm
    assert "dev/fuse" not in lxc_config(ContainerSpec("plain"), "br0")


def make_lxc(tmp_path, state="RUNNING", pools=("default", "fast"), log=None):
    def run(cmd):
        if log is not None:
            log.append(cmd)
        if cmd[0] == "lxc-info":
            return 0, f"State: {state}\n"
        if cmd[0] == "cp":
            import shutil

            shutil.copytree(cmd[-2], cmd[-1])
        if cmd[:2] == ["rm", "-rf"]:
            import shutil

            shutil.rmtree(cmd[2], ignore_errors=True)
        return 0, ""

    cg = tmp_path / "cgroup"
    return LxcContainers(lambda p: str(tmp_path / p), lambda n: "br0", run, lambda: list(pools),
                         cgroup_root=cg)  # fmt: skip


def put(tmp_path, name, pool="default"):
    home = tmp_path / pool / "containers" / name
    (home / "snaps/s1").mkdir(parents=True)
    spec = ContainerSpec(name, pool=pool)
    (home / "ibhics.json").write_text(json.dumps(spec.public() | {"password": ""}))
    (home / "config").write_text(lxc_config(spec, "br0", f"{home}/rootfs.img"))
    (home / "snaps/s1/config").write_text(f"lxc.rootfs.path = loop:{home}/snaps/s1/rootfs\n")
    return home


def test_memory_use_from_cgroup_v2(tmp_path):
    lxc = make_lxc(tmp_path)
    home = put(tmp_path, "web")
    (tmp_path / "cgroup/lxc.payload.web").mkdir(parents=True)
    (tmp_path / "cgroup/lxc.payload.web/memory.current").write_text(str(300 * 2**20))
    info = lxc.get("web")
    assert info.memory_used_mb == 300 and info.path == str(home)
    assert info.to_dict()["path"] == str(home)


def test_move_to_another_pool(tmp_path):
    put(tmp_path, "web")
    assert "stop" in str(_raises(lambda: make_lxc(tmp_path).move("web", "fast")))
    lxc = make_lxc(tmp_path, state="STOPPED")
    info = lxc.move("web", "fast")
    new = tmp_path / "fast/containers/web"
    assert info.spec.pool == "fast" and info.path == str(new)
    assert not (tmp_path / "default/containers/web").exists()
    assert f"loop:{new}/rootfs.img" in (new / "config").read_text()
    assert f"{new}/snaps/s1/rootfs" in (new / "snaps/s1/config").read_text()
    assert json.loads((new / "ibhics.json").read_text())["pool"] == "fast"
    assert "no storage pool" in str(_raises(lambda: lxc.move("web", "nope")))


def _raises(fn):
    try:
        fn()
    except Exception as err:  # noqa: BLE001
        return err
    raise AssertionError("did not raise")


def test_start_rewrites_config_with_new_settings(tmp_path):
    home = put(tmp_path, "dock")
    meta = json.loads((home / "ibhics.json").read_text()) | {"nesting": True}
    (home / "ibhics.json").write_text(json.dumps(meta))
    log = []
    make_lxc(tmp_path, state="STOPPED", log=log).action("dock", "start")
    assert "dev/fuse" in (home / "config").read_text()


def test_exec_jobs_stream_and_stop():
    jobs = ExecJobs()
    jid = jobs.start([sys.executable, "-c", "print('one', flush=True); print('two')"])
    for _ in range(200):
        r = jobs.read(jid)
        if r["done"]:
            break
        time.sleep(0.02)
    assert r["done"] and r["code"] == 0 and "one\ntwo" in r["output"]
    assert jobs.read(jid, r["offset"])["output"] == ""
    slow = jobs.start([sys.executable, "-c", "import time; time.sleep(30)"])
    jobs.stop(slow)
    for _ in range(200):
        if jobs.read(slow)["done"]:
            break
        time.sleep(0.02)
    assert jobs.read(slow)["done"] and jobs.read(slow)["code"] != 0


def test_api_jobs_and_move():
    client = TestClient(create_app(FakeDriver()))
    client.post("/api/v1/containers", json={"name": "web"})
    wait_for(client, "web")
    job = client.post("/api/v1/containers/web/jobs", json={"command": "apt update"}).json()["job"]
    r = client.get(f"/api/v1/containers/web/jobs/{job}").json()
    assert r["done"] and "apt update" in r["output"]
    assert client.delete(f"/api/v1/containers/web/jobs/{job}").status_code == 204
    assert client.post("/api/v1/containers/web/move", json={"pool": "nope"}).status_code == 422
    assert client.post("/api/v1/containers/web/move", json={"pool": "default"}).status_code == 200


def test_fake_move():
    from ibhics.hypervisor.containers import FakeContainers

    cts = FakeContainers(lambda: {"default"}, lambda: {"default", "fast"})
    cts.create(ContainerSpec("web"))
    assert "stop" in str(_raises(lambda: cts.move("web", "fast")))
    cts.action("web", "stop")
    assert cts.move("web", "fast").spec.pool == "fast"
