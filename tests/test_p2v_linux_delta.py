"""Execute the embedded Linux delta client against fake devices and manager APIs."""

import builtins
import json
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ibhics.manager.p2v import AGENT
from tests.test_p2v import env as p2v_env
from tests.test_p2v import inventory

env = p2v_env


@pytest.mark.parametrize("mode", ["live", "lvm", "changed", "overflow", "create-failed"])
def test_linux_delta_snapshot_and_live_protocol(env, tmp_path, monkeypatch, mode):
    client, _, sessions = env
    opened = client.post("/api/v1/p2v/sessions", json={"pool": "default"}).json()
    sid = opened["session"]["id"]
    token = opened["agent_token"]
    original = tmp_path / "source"
    original.write_bytes(b"a" * (1 << 20))
    machine = inventory(1 << 20)
    monkeypatch.setenv("IBHICS_P2V_API", "https://manager/api/v1/p2v/upload/" + sid)
    monkeypatch.setenv("IBHICS_P2V_TOKEN", token)
    monkeypatch.setenv("IBHICS_P2V_PIN", "PIN")
    monkeypatch.setenv("IBHICS_P2V_MACHINE", json.dumps(machine))
    monkeypatch.setattr(sys, "argv", ["delta", "sda:1048576"])
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    real_open = builtins.open

    def disk_open(path, *args, **kwargs):
        return real_open(original if str(path).startswith("/dev/") else path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", disk_open)
    commands = []

    def check_output(args, **kwargs):
        commands.append(args)
        if args[0] == "lvs":
            if mode in ("live", "changed"):
                raise subprocess.CalledProcessError(1, args)
            if "lv_attr" in args:
                return "swi-I-s---" if mode == "overflow" else "swi-a-s---"
            return "vg"
        if args[0] == "vgs":
            return str(2 << 30)
        if args[0] == "lvcreate" and mode == "create-failed":
            raise subprocess.CalledProcessError(1, args)
        return ""

    def curl(args, input, **kwargs):
        assert args[0] == "curl" and args[args.index("--pinnedpubkey") + 1] == "sha256//PIN"
        assert "X-P2V-Token: " + token in args
        path = args[-1].removeprefix("https://manager")
        method = args[args.index("-X") + 1]
        response = client.request(
            method,
            path,
            content=input,
            headers={"X-P2V-Token": token, "Content-Type": "application/json"},
        )
        if mode == "changed" and path.endswith("/begin"):
            original.write_bytes(b"b" * (1 << 20))
        return SimpleNamespace(
            returncode=0 if response.status_code < 300 else 22, stdout=response.content
        )

    monkeypatch.setattr(subprocess, "check_output", check_output)
    monkeypatch.setattr(subprocess, "run", curl)
    code = AGENT.read_text().split("<<'PYDELTA'\n", 1)[1].split("\nPYDELTA", 1)[0]
    if mode in ("changed", "overflow", "create-failed"):
        with pytest.raises(SystemExit) as error:
            exec(compile(code, str(AGENT), "exec"), {})
        assert error.value.code == 1
        assert sessions.get(sid).state == "waiting"
    else:
        exec(compile(code, str(AGENT), "exec"), {})
        assert sessions.get(sid).state == "ready"
    if mode in ("lvm", "overflow", "create-failed"):
        assert any(c[0] == "lvcreate" for c in commands)
        cleanup = [c for c in commands if c[0] == "lvremove"]
        assert len(cleanup) == 1 and cleanup[0][-1].startswith("/dev/vg/ibhics-repl-")
    else:
        assert not any(c[0] in ("lvcreate", "lvremove") for c in commands)
