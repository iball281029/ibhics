"""Isolated local Codex adapter for previews and cluster-bound AI license passes."""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

from fastapi import HTTPException

from .common import ChatIn, prompt_messages, redact
from .gateway import Usage

DISABLED = (
    "shell_tool",
    "unified_exec",
    "apps",
    "browser_use",
    "computer_use",
    "multi_agent",
    "multi_agent_v2",
    "code_mode_host",
    "view_image",
    "image_generation",
    "memories",
    "daemon_auto_start",
)


AI_PASS_EXPIRED = "The AI pass in this license has expired. Contact your vendor for a new license."


class CodexLocalPreview:
    """Run tool-free Codex with a bounded quota and an optional explicit credential home."""

    def __init__(
        self,
        state_dir,
        *,
        enabled=False,
        binary=None,
        timeout=75,
        daily_limit=10,
        codex_home=None,
    ):
        self.codex_home = Path(codex_home) if codex_home else None
        self.auth_failed = False
        self.enabled = enabled
        self.binary = binary or shutil.which("codex")
        self.timeout, self.daily_limit = timeout, daily_limit
        self.usage = Usage(Path(state_dir) / "usage.sqlite")
        self.lock = threading.Lock()

    def configured(self):
        return bool(self.enabled and self.binary)

    def chat(self, payload):
        if not self.configured():
            raise HTTPException(503, "Contact Support to provision the AI service.")
        if not self.lock.acquire(blocking=False):
            raise HTTPException(429, "A preview question is already running. Please wait.")
        try:
            body = ChatIn.model_validate(
                {
                    key: payload[key]
                    for key in ("message", "history", "include_context", "language")
                    if key in payload
                }
            )
            # Keep history bounded for both local previews and license-backed requests.
            body.history = body.history[-4:]
            system, messages = prompt_messages(
                body, payload.get("snapshot", {}) if body.include_context else {}
            )
            # Make the selected language explicit in the CLI's user prompt too.
            # The UI locale must not inherit the operator account's usual language.
            language = "Thai" if body.language == "th" else "English"
            messages[-1]["content"] = (
                f"Reply in {language}, unless I explicitly request another language.\n"
                + messages[-1]["content"]
            )
            system += "\nKeep the answer under 250 words. Do not use any tools or read files."
            quota = self.usage.reserve("local-codex-preview", self.daily_limit)
            with tempfile.TemporaryDirectory(prefix="ibhics-codex-preview-") as directory:
                root = Path(directory)
                instructions, answer = root / "instructions.txt", root / "answer.txt"
                instructions.write_text(system, encoding="utf-8")
                instructions.chmod(0o600)
                command = [
                    self.binary,
                    "exec",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "read-only",
                ]
                for feature in DISABLED:
                    command += ["--disable", feature]
                for config in (
                    'web_search="disabled"',
                    'approval_policy="never"',
                    "project_doc_max_bytes=0",
                    'model_reasoning_effort="low"',
                    "model_instructions_file=" + json.dumps(str(instructions)),
                ):
                    command += ["-c", config]
                command += [
                    "--color",
                    "never",
                    "--cd",
                    str(root),
                    "--output-last-message",
                    str(answer),
                    "-",
                ]
                # Do not inherit the parent agent's thread identity or service API secrets.
                allowed = (
                    "PATH",
                    "HOME",
                    "LANG",
                    "LC_ALL",
                    "TMPDIR",
                    "CODEX_HOME",
                    "SSL_CERT_FILE",
                    "SSL_CERT_DIR",
                    "HTTPS_PROXY",
                    "HTTP_PROXY",
                    "NO_PROXY",
                )
                environment = {key: os.environ[key] for key in allowed if key in os.environ}
                if self.codex_home:
                    environment["CODEX_HOME"] = str(self.codex_home)
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE if self.codex_home else subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    cwd=root,
                    env=environment,
                    start_new_session=True,
                )
                try:
                    _, error = process.communicate(
                        json.dumps(messages, ensure_ascii=False), timeout=self.timeout
                    )
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                    raise HTTPException(
                        503, "AI service is unavailable. Check connectivity or contact Support."
                    ) from None
                if self.codex_home and process.returncode != 0:
                    # Inspect only in memory. Never expose CLI stderr (it can contain secrets).
                    detail = (error or "").lower()
                    if process.returncode in (401, 403) or any(
                        marker in detail
                        for marker in (
                            "401",
                            "unauthorized",
                            "authentication",
                            "not logged in",
                            "please log in",
                            "refresh_token",
                            "refresh token",
                            "token expired",
                            "token has expired",
                            "invalid token",
                            "invalid_grant",
                            "auth token",
                            "login required",
                        )
                    ):
                        self.auth_failed = True
                        raise HTTPException(503, AI_PASS_EXPIRED)
                if process.returncode != 0 or not answer.is_file():
                    raise HTTPException(
                        503, "AI service is unavailable. Check connectivity or contact Support."
                    )
                reply = answer.read_text(encoding="utf-8").strip()
                if not reply:
                    raise HTTPException(
                        503, "AI service is unavailable. Check connectivity or contact Support."
                    )
                return {"reply": redact(reply[:12000]), "allowance": quota}
        except OSError as exc:
            raise HTTPException(
                503, "AI service is unavailable. Check connectivity or contact Support."
            ) from exc
        finally:
            self.lock.release()
