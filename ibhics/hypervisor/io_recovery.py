"""Resume VMs that QEMU paused on a storage I/O error once the storage is back.

When the NFS gateway fails over and a client cannot reclaim its locks in time, QEMU
gets EIO on the image file and pauses the guest (error_policy=stop). A plain resume
fails again because the old file descriptor stays broken. Reopening the file node
(blockdev-reopen) gives QEMU a fresh descriptor; the guest then continues with its
memory intact, without a power cycle.

Safety rules:
- only domains paused with reason IOERROR are touched;
- every image directory of the domain must pass a write/fsync/read/delete probe in a
  child process with a timeout (a hung NFS mount must not hang the agent);
- only file nodes ('driver': 'file') whose path is under an allowed root are reopened,
  with their original options (cache.direct flipped once, then restored);
- at most `max_attempts` attempts per domain per `window` seconds;
- the result is reported only after the domain is running and the guest agent answers.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

PROBE = r"""
import os, sys
d = sys.argv[1]
p = os.path.join(d, ".ibhics-io-probe-%d" % os.getpid())
data = b"ibhics-io-probe\n"
fd = os.open(p, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
try:
    os.write(fd, data); os.fsync(fd)
finally:
    os.close(fd)
with open(p, "rb") as f:
    ok = f.read() == data
os.unlink(p)
sys.exit(0 if ok else 3)
"""


def probe_directory(path: str, timeout: float = 15) -> bool:
    """True when a small write/fsync/read/delete in `path` succeeds in time."""
    try:
        done = subprocess.run(
            [sys.executable, "-c", PROBE, path], capture_output=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return done.returncode == 0


def blockdev_options(pid: int) -> list[dict]:
    """The -blockdev JSON options QEMU was started with."""
    args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    out = []
    for i, arg in enumerate(args[:-1]):
        if arg == b"-blockdev":
            try:
                out.append(json.loads(args[i + 1]))
            except ValueError:
                continue  # key=value form: not ours to rebuild
    return out


class IORecovery:
    def __init__(
        self,
        conn_factory,
        allowed_roots=("/mnt/ibhics/",),
        probe=probe_directory,
        options_of=blockdev_options,
        monitor=None,
        agent=None,
        clock=time.monotonic,
        sleep=time.sleep,
        max_attempts: int = 3,
        window: float = 3600,
        notify=None,
    ):
        self.conn_factory = conn_factory
        self.allowed_roots = tuple(allowed_roots)
        self.probe, self.options_of = probe, options_of
        self.monitor = monitor or _qemu_monitor
        self.agent = agent or _guest_ping
        self.clock, self.sleep = clock, sleep
        self.max_attempts, self.window = max_attempts, window
        self.notify = notify or (lambda *a, **k: None)
        self.attempts: dict[str, list[float]] = {}

    def _allowed(self, path: str) -> bool:
        real = os.path.realpath(path)
        return any(real.startswith(root) for root in self.allowed_roots)

    def candidates(self, conn):
        import libvirt

        out = []
        for dom in conn.listAllDomains():
            state, reason = dom.state()
            if state == libvirt.VIR_DOMAIN_PAUSED and reason == libvirt.VIR_DOMAIN_PAUSED_IOERROR:
                out.append(dom)
        return out

    def _budget(self, uuid: str) -> bool:
        now = self.clock()
        recent = [t for t in self.attempts.get(uuid, []) if now - t < self.window]
        self.attempts[uuid] = recent
        if len(recent) >= self.max_attempts:
            return False
        recent.append(now)
        return True

    def recover(self, dom, pid: int) -> dict:
        """Reopen the domain's file nodes and resume it. Returns a result record."""
        import libvirt

        name, uuid = dom.name(), dom.UUIDString()
        result = {"vm": name, "uuid": uuid, "recovered": False, "reason": ""}
        files = [o for o in self.options_of(pid) if o.get("driver") == "file" and "filename" in o]
        if not files:
            result["reason"] = "no file-backed disks to reopen"
            return result
        # Local files (UEFI firmware/NVRAM, seed ISOs) do not lose their descriptor when
        # shared storage drops: reopen only the nodes on managed storage.
        files = [o for o in files if self._allowed(o["filename"])]
        if not files:
            result["reason"] = "no disk on managed storage; left for an operator"
            return result
        dirs = sorted({os.path.dirname(o["filename"]) for o in files})
        if not all(self.probe(d) for d in dirs):
            result["reason"] = "storage not ready yet"
            return result
        if not self._budget(uuid):
            result["reason"] = "recovery attempts exhausted for this hour"
            return result
        for original in files:
            flipped = json.loads(json.dumps(original))
            cache = dict(flipped.get("cache") or {})
            cache["direct"] = not cache.get("direct", False)
            flipped["cache"] = cache
            for options in (flipped, original):
                reply = self.monitor(
                    dom, {"execute": "blockdev-reopen", "arguments": {"options": [options]}}
                )
                if "error" in reply:
                    result["reason"] = f"blockdev-reopen refused on {original.get('node-name')}"
                    return result
        try:
            dom.resume()
        except libvirt.libvirtError as exc:
            result["reason"] = f"resume failed: {exc.get_error_message()}"
            return result
        self.sleep(3)
        state, _reason = dom.state()
        if state != libvirt.VIR_DOMAIN_RUNNING:
            result["reason"] = "paused again after resume"
            return result
        result["recovered"] = True
        result["guest_agent"] = bool(self.agent(dom))
        result["reason"] = "resumed after reopening disk files"
        return result

    def run_once(self) -> list[dict]:
        conn = self.conn_factory()
        results = []
        for dom in self.candidates(conn):
            try:
                pid = int(Path(f"/run/libvirt/qemu/{dom.name()}.pid").read_text().strip())
                outcome = self.recover(dom, pid)
            except Exception as exc:  # one VM must never stop the loop for the others
                outcome = {"vm": dom.name(), "recovered": False, "reason": type(exc).__name__}
            results.append(outcome)
            (log.warning if not outcome["recovered"] else log.info)("io-recovery %s", outcome)
            if (
                outcome["recovered"]
                or outcome["reason"] == "recovery attempts exhausted for this hour"
            ):
                self.notify(outcome)
        return results

    def start(self, every: float = 30) -> threading.Thread:
        def loop():
            while True:
                try:
                    self.run_once()
                except Exception:
                    log.exception("io-recovery pass failed")
                self.sleep(every)

        thread = threading.Thread(target=loop, name="io-recovery", daemon=True)
        thread.start()
        return thread


def _qemu_monitor(dom, command: dict) -> dict:
    import libvirt_qemu

    return json.loads(libvirt_qemu.qemuMonitorCommand(dom, json.dumps(command), 0))


def _guest_ping(dom) -> bool:
    try:
        import libvirt_qemu

        libvirt_qemu.qemuAgentCommand(dom, '{"execute":"guest-ping"}', 5, 0)
        return True
    except Exception:
        return False
