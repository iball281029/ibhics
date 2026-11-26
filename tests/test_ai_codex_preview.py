"""Codex is an explicit local test adapter with no production factory or credential copying."""

import json
import signal
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from ibhics.ai.codex_preview import DISABLED, CodexLocalPreview


def test_disabled_adapter_never_launches_codex(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("Must not launch"))
    adapter = CodexLocalPreview(tmp_path, binary="/stub/codex")
    assert not adapter.configured()
    with pytest.raises(HTTPException) as error:
        adapter.chat({"message": "Help"})
    assert error.value.status_code == 503


def test_local_prompt_is_isolated_tools_disabled_and_budget_persisted(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-to-be-inherited")
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread-not-to-be-inherited")

    class Process:
        pid, returncode = 1234, 0

        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))
            self.command = command

        def communicate(self, text, timeout):
            content = json.loads(text)[-1]["content"]
            assert content.startswith("Reply in English") and content.endswith("Help")
            answer = Path(self.command[self.command.index("--output-last-message") + 1])
            answer.write_text("Useful guidance", encoding="utf-8")
            return None, None

    monkeypatch.setattr(subprocess, "Popen", Process)
    adapter = CodexLocalPreview(tmp_path, enabled=True, binary="/stub/codex", daily_limit=1)
    result = adapter.chat({"message": "Help"})
    assert result["reply"] == "Useful guidance" and result["allowance"]["used"] == 1
    command, options = calls[0]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in command and "--ephemeral" in command
    for feature in DISABLED:
        assert any(command[i : i + 2] == ["--disable", feature] for i in range(len(command) - 1))
    assert options["start_new_session"] and "shell" not in options
    assert options["stdout"] == subprocess.DEVNULL and options["stderr"] == subprocess.DEVNULL
    assert "ANTHROPIC_API_KEY" not in options["env"] and "CODEX_THREAD_ID" not in options["env"]
    assert not Path(options["cwd"]).exists()  # temporary prompt and answer were cleaned
    again = CodexLocalPreview(tmp_path, enabled=True, binary="/stub/codex", daily_limit=1)
    with pytest.raises(HTTPException) as error:
        again.chat({"message": "Help"})
    assert error.value.status_code == 429 and len(calls) == 1


def test_timeout_terminates_the_process_group_and_releases_lock(tmp_path, monkeypatch):
    signals = []

    class Process:
        pid, returncode, count = 1234, 0, 0

        def __init__(self, *args, **kwargs):
            pass

        def communicate(self, *args, **kwargs):
            self.count += 1
            if self.count < 3:
                raise subprocess.TimeoutExpired("codex", 1)
            return None, None

    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setattr("os.killpg", lambda pid, sig: signals.append((pid, sig)))
    adapter = CodexLocalPreview(tmp_path, enabled=True, binary="/stub/codex", timeout=1)
    with pytest.raises(HTTPException) as error:
        adapter.chat({"message": "Help"})
    assert error.value.status_code == 503
    assert signals == [(1234, signal.SIGTERM), (1234, signal.SIGKILL)]
    assert adapter.lock.acquire(blocking=False)
    adapter.lock.release()
