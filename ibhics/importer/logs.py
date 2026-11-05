"""Pure virt-v2v log parsing and progress sampling (no file or process access)."""

from __future__ import annotations

import re

STEP = re.compile(r"^\[\s*(\d+(?:\.\d+)?)\]\s*(.+)$")
# virt-v2v spins through block elements (U+2580-259F).
PERCENT = re.compile(r"^[▀-▟|/\\\-\s]*(\d{1,3}(?:\.\d+)?)%\s*\[[*#=\-\s]*\]?\s*$")
FRAGMENT = re.compile(r"^[*\-\s]+\]\s*$")
ERROR = re.compile(r"error|exception|failed|traceback", re.IGNORECASE)


def log_lines(text: str) -> list[str]:
    """Treat terminal carriage returns as complete updates, including CRLF."""
    return text.replace("\r\n", "\n").replace("\r", "\n").splitlines()


def progress_percent(line: str) -> float | None:
    match = PERCENT.fullmatch(line.strip())
    return min(float(match[1]), 100) if match else None


def clean_v2v_log(text: str) -> str:
    return "\n".join(
        line
        for line in log_lines(text)
        if progress_percent(line) is None and not FRAGMENT.fullmatch(line.strip())
    )


def _kind(text: str) -> str:
    for kind, prefixes in (
        ("source", ("Setting up the source", "Opening the source")),
        ("inspect", ("Inspecting", "Checking for sufficient")),
        ("convert", ("Converting",)),
        ("copy", ("Copying disk",)),
        ("finish", ("Creating output metadata", "Finishing off")),
    ):
        if text.startswith(prefixes):
            return kind
    return "other"


def parse_v2v_log(text: str) -> dict:
    """Summarize stages and diagnostics, keeping progress tied to its own stage."""
    result = dict(steps=[], progress=None, warnings=[], notes=[], errors=[], os=None, disks=None)
    continuation = None
    for raw in log_lines(text):
        line = raw.strip()
        pct = progress_percent(line)
        if pct is not None:
            if result["steps"]:
                result["progress"] = {"percent": pct, "step": result["steps"][-1]["text"]}
            continue
        if FRAGMENT.fullmatch(line) or not line:
            if not line:
                continuation = None
            continue
        match = STEP.match(line)
        if match:
            stage = match[2]
            result["steps"].append({"t": float(match[1]), "text": stage, "kind": _kind(stage)})
            result["progress"] = None
            continuation = None
            os_match = re.match(r"Converting (.+) to run on KVM", stage)
            if os_match:
                result["os"] = os_match[1]
            disk = re.search(r"Copying disk (\d+/\d+)", stage)
            if disk:
                result["disks"] = disk[1]
            continue
        if line.startswith("virt-v2v: warning:"):
            continuation = "warnings"
        elif line.startswith("virt-v2v: error:") or ERROR.search(line):
            continuation = "errors"
        elif line.startswith("virt-v2v:"):
            continuation = "notes"
        result[continuation or "notes"].append(raw)
    return result


class ProgressSampler:
    """Keep the first update, then a 5 percentage-point change or 10 elapsed seconds.

    Callers provide monotonic seconds so the reducer is deterministic in tests.
    Reset at every stage, since each disk's percentage starts again at zero.
    """

    def __init__(self) -> None:
        self.percent = None
        self.time = None

    def keep(self, line: str, now: float) -> bool:
        if STEP.match(line.strip()):
            self.percent = self.time = None
        pct = progress_percent(line)
        if pct is None:
            return not FRAGMENT.fullmatch(line.strip())
        if self.percent is None or abs(pct - self.percent) >= 5 or now - self.time >= 10:
            self.percent, self.time = pct, now
            return True
        return False
