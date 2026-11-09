"""Read-only hardware observations. Missing telemetry never means healthy."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path


def read(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return p.returncode, p.stdout
    except FileNotFoundError:
        return 127, ""
    except (OSError, subprocess.TimeoutExpired):
        return 124, ""


def item(category, name, status, detail, source):
    return dict(category=category, name=name, status=status, detail=detail, source=source)


def sensors(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        fields = [s.strip() for s in line.split("|")]
        if len(fields) != 5:
            continue
        name, _, state, _, detail = fields
        lower = name.lower()
        category = (
            "Temperature"
            if "temp" in lower
            else "Fans"
            if "fan" in lower
            else "Power supplies"
            if re.search(r"\bps\d|power supply|\bpsu", lower)
            else "Voltage"
            if "volt" in detail.lower()
            else "Other sensors"
        )
        state = state.lower()
        status = {"ok": "ok", "nc": "warning", "cr": "critical", "nr": "critical"}.get(
            state, "unknown"
        )
        # Discrete sensors can say 'ok' while their asserted bits describe a fault.
        if re.search(r"failure|failed|uncorrectable|ac lost|power lost|overheat", detail, re.I):
            status = "critical"
        elif re.search(r"predictive|degraded|non-critical", detail, re.I):
            status = "warning"
        if re.search(r"no reading|not readable|event-only|disabled", detail, re.I):
            status = "unknown"
        out.append(item(category, name, status, detail, "IPMI SDR"))
    return out


def memory(root: Path) -> list[dict]:
    out = []
    for mc in sorted(root.glob("sys/devices/system/edac/mc/mc[0-9]*")):
        try:
            ce = int((mc / "ce_count").read_text())
            ue = int((mc / "ue_count").read_text())
        except (OSError, ValueError):
            continue
        status = "critical" if ue else "warning" if ce else "ok"
        out.append(
            item(
                "Memory",
                f"RAM controller {mc.name}",
                status,
                f"{ce} corrected / {ue} uncorrected ECC errors since counter reset",
                "Linux EDAC",
            )
        )
        for dimm in sorted(mc.glob("dimm[0-9]*")):
            try:
                if int((dimm / "size").read_text()) <= 0:
                    continue
                ce = int((dimm / "dimm_ce_count").read_text())
                ue = int((dimm / "dimm_ue_count").read_text())
                label = (dimm / "dimm_label").read_text().strip() or dimm.name
            except (OSError, ValueError):
                continue
            out.append(
                item(
                    "Memory",
                    label,
                    "critical" if ue else "warning" if ce else "ok",
                    f"{ce} corrected / {ue} uncorrected ECC errors since counter reset",
                    "Linux EDAC",
                )
            )
    return out or [
        item(
            "Memory",
            "RAM error reporting",
            "unknown",
            "Linux EDAC counters unavailable. DIMM temperature is not a memory test.",
            "Linux EDAC",
        )
    ]


def disk_observation(device: dict, run=read) -> dict:
    path = device["path"]
    code, text = run(["smartctl", "-H", "-A", "-i", "-j", path])
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict) or not data:
        return item("Disks", path, "unknown", "SMART query unavailable or timed out", "SMART")
    passed = (data.get("smart_status") or {}).get("passed")
    status = "ok" if passed is True else "critical" if passed is False else "unknown"
    notes = [
        "SMART passed"
        if passed is True
        else "SMART failing"
        if passed is False
        else "SMART health unavailable"
    ]
    # Exit status is a bitmask: error bits must not become a healthy observation.
    if code & 7:
        status = "unknown" if status == "ok" else status
        notes.append("SMART query incomplete")
    if code & 8:
        status = "critical"
    if code & 240 and status == "ok":
        status = "warning"
        notes.append("SMART attributes or error/self-test history need review")
    for attr in (data.get("ata_smart_attributes") or {}).get("table", []):
        if attr.get("name") in (
            "Reallocated_Sector_Ct",
            "Current_Pending_Sector",
            "Offline_Uncorrectable",
        ):
            value = (attr.get("raw") or {}).get("value", 0)
            if isinstance(value, (int, float)) and value > 0:
                notes.append(f"{attr['name']}: {value}")
                if status == "ok":
                    status = "warning"
    temp = (data.get("temperature") or {}).get("current")
    if temp is not None:
        notes.append(f"{temp} °C")
    model = data.get("model_name") or device.get("model") or ""
    return item("Disks", f"{path} · {model}".strip(" ·"), status, "; ".join(notes), "SMART")


def disks(run=read) -> list[dict]:
    code, text = run(["lsblk", "-J", "-b", "-d", "-o", "PATH,TYPE,SIZE,MODEL"])
    try:
        devices = json.loads(text).get("blockdevices", []) if not code else []
        devices = [
            d
            for d in devices
            if d.get("type") == "disk"
            and re.fullmatch(r"/dev/(sd[a-z]+|hd[a-z]+|vd[a-z]+|nvme\d+n\d+)", d.get("path", ""))
            and int(d.get("size") or 0) > 0
        ]
    except (ValueError, TypeError, AttributeError):
        devices = []
    if not devices:
        return [
            item(
                "Disks",
                "Physical disk health",
                "unknown",
                "No supported disks discovered. RAID controllers may hide physical drives.",
                "SMART",
            )
        ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(lambda d: disk_observation(d, run), devices))


def bmc(run=read) -> tuple[list[dict], list[dict], str]:
    code, text = run(["ipmitool", "-I", "open", "sdr", "elist", "all"])
    observed = sensors(text)
    if code:
        observed = []  # do not present partial output from a failed query as complete
    if not observed:
        observed = [
            item(
                "BMC",
                "Hardware sensors",
                "unknown",
                "Install ipmitool and check the local IPMI interface; query failed or timed out.",
                "IPMI SDR",
            )
        ]
    code, text = run(["ipmitool", "-I", "open", "sel", "elist", "last", "30"])
    events = []
    if not code:
        for line in text.splitlines():
            if (
                not re.search(
                    r"memory|processor|fan|power supply|temperature|voltage|drive|watchdog",
                    line,
                    re.I,
                )
                or "|" not in line
            ):
                continue
            events.append({"detail": line.strip()[:800], "source": "BMC SEL history"})
    return observed, events, "unavailable" if code else "available"


def collect(root=Path("/"), run=read) -> dict:
    with ThreadPoolExecutor(max_workers=2) as pool:
        bmc_job = pool.submit(bmc, run)
        disk_job = pool.submit(disks, run)
        observed, events, event_status = bmc_job.result()
        observed += disk_job.result() + memory(root)
    for category in ("Fans", "Power supplies", "Temperature"):
        if not any(i["category"] == category for i in observed):
            observed.append(
                item(category, category, "unknown", "No telemetry reported", "IPMI SDR")
            )
    return {
        "items": observed,
        "events": events,
        "events_status": event_status,
        "collected_at": datetime.now(UTC).isoformat(),
        "refresh_seconds": 60,
        "collecting": False,
    }


class Monitor:
    """Slow BMC/SMART reads run outside HTTP and networking locks."""

    def __init__(self, collector=collect):
        self.collector = collector
        self.lock = threading.Lock()
        self.value = {"items": [], "events": [], "collected_at": None, "refresh_seconds": 60}
        self.updated = 0.0
        self.running = False

    def snapshot(self):
        with self.lock:
            if not self.running and (not self.updated or time.monotonic() - self.updated >= 60):
                self.running = True
                threading.Thread(target=self._collect, daemon=True).start()
            return {**self.value, "collecting": self.running}

    def _collect(self):
        try:
            value = self.collector()
        except Exception:
            value = {
                "items": [
                    item(
                        "System",
                        "Hardware query",
                        "unknown",
                        "Collection failed; previous status cannot be confirmed",
                        "Node",
                    )
                ],
                "events": [],
                "events_status": "unavailable",
                "collected_at": datetime.now(UTC).isoformat(),
                "refresh_seconds": 60,
            }
        with self.lock:
            self.value = value
            self.updated = time.monotonic()
            self.running = False
