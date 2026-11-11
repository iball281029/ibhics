import os
import subprocess
from pathlib import Path

import pytest

from ibhics.installer import arbiter


def test_arbiter_firstboot_is_diskless_witness(monkeypatch):
    captured = []
    s = arbiter.s
    monkeypatch.setattr(s, "detect_interfaces", lambda: ["ens192"])
    monkeypatch.setattr(s, "wake_ports", lambda _: None)
    monkeypatch.setattr(s, "ask", lambda label, default="", *a, **kw: default)
    monkeypatch.setattr(s, "ask_password", lambda _: "test-console-password")
    monkeypatch.setattr(s, "apply", lambda cfg, password, admin: captured.append((cfg, admin)))
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert arbiter.main() == 0
    cfg, admin = captured[0]
    assert cfg.role == "witness" and not cfg.runs_vms
    assert cfg.hostname == "arbiter-01" and cfg.address == "172.16.12.105/24"
    assert len(cfg.token) >= 32 and admin is None
    cfg.validate()


@pytest.mark.parametrize(
    "sizes,removable,expected",
    [
        ([], [], None),
        ([40], [0], "/dev/vda"),
        ([40, 40], [0, 0], None),
        ([8], [0], None),
        ([40, 40], [0, 1], "/dev/vda"),
    ],
)
def test_disk_picker_requires_one_suitable_disk(tmp_path, sizes, removable, expected):
    tools = tmp_path / "bin"
    tools.mkdir()
    names = [f"vd{chr(97 + i)}" for i in range(len(sizes))]
    listing = tools / "list-devices"
    listing.write_text("#!/bin/sh\n" + "\n".join(f"echo /dev/{n}" for n in names) + "\n")
    listing.chmod(0o755)
    sys = tmp_path / "sys"
    for name, size, rm in zip(names, sizes, removable, strict=True):
        entry = sys / name
        entry.mkdir(parents=True)
        (entry / "size").write_text(str(size * 1024**3 // 512))
        (entry / "removable").write_text(str(rm))
    script = Path(__file__).parents[1] / "installer/arbiter/pick-disk.sh"
    result = subprocess.run(
        ["sh", str(script)],
        capture_output=True,
        text=True,
        env=os.environ | {"SYS": str(sys), "PATH": str(tools) + ":" + os.environ["PATH"]},
    )
    if expected is None:
        assert result.returncode != 0 and not result.stdout.strip()
    else:
        assert result.returncode == 0 and result.stdout.strip() == expected


def test_setup_failure_is_visible_and_retried(monkeypatch, capsys):
    calls = []

    def run():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("system bus unavailable")
        return 0

    monkeypatch.setattr(arbiter, "setup_once", run)
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert arbiter.main() == 0
    assert len(calls) == 2
    text = capsys.readouterr().out
    assert "setup did not finish" in text and "has not been marked ready" in text
