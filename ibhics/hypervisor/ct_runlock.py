"""Detached POSIX lock holders. Never unlink the shared lock inode.

The local control directory must be host-owned, outside the guest user namespace.
NFS opens happen only in the child: a stuck server cannot hang the start hook.
This advisory lock plus periodic self-fencing does not provide STONITH: after
server-side lock revocation an old loop device may write before it is stopped.
"""

from __future__ import annotations

import fcntl
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

RUN_DIR = Path("/run/ibhics/ct-locks")
TIMEOUT = 8
STALE = 15


def _paths(home, run_dir):
    home = Path(os.path.abspath(home))
    if not home.name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in home.name):
        raise ValueError("Invalid container name")
    directory = Path(run_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return home, directory / (home.name + ".pid")


def _alive(data):
    try:
        os.kill(data["pid"], 0)
        # Never mistake a reused PID (or zombie) for our detached holder.
        proc = Path(f"/proc/{data['pid']}")
        if Path("/proc/self/stat").exists():
            command = (proc / "cmdline").read_bytes().split(b"\0")
            if data["token"].encode() not in command:
                return False
        return True
    except (OSError, ValueError, KeyError):
        return False


def healthy(home, run_dir=RUN_DIR):
    home, pidfile = _paths(home, run_dir)
    try:
        data = json.loads(pidfile.read_text())
        return (
            _alive(data)
            and data["home"] == str(home)
            and time.monotonic() - data["at"] < STALE
            and not pidfile.with_suffix(".release").exists()
        )
    except (OSError, ValueError, KeyError):
        return False


def alive(home, run_dir=RUN_DIR):
    """The holder process exists, whatever its heartbeat age (NFS may be stalled)."""
    home, pidfile = _paths(home, run_dir)
    try:
        data = json.loads(pidfile.read_text())
        return _alive(data) and data["home"] == str(home)
    except (OSError, ValueError, KeyError):
        return False


def acquire(home, run_dir=RUN_DIR, timeout=TIMEOUT):
    home, pidfile = _paths(home, run_dir)
    # Serialize helpers on this host, including an explicit pre-acquire + hook.
    with pidfile.with_suffix(".guard").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        if healthy(home, run_dir):
            return
        if pidfile.exists():
            data = {}
            try:
                data = json.loads(pidfile.read_text())
            except (OSError, ValueError):
                pass
            if _alive(data):
                raise RuntimeError(
                    "Container run lock holder is unhealthy; fence it before retrying"
                )
            pidfile.unlink(missing_ok=True)  # the holder died: its lock is gone with it
        token = uuid4().hex
        request = pidfile.with_suffix(".request")
        request.write_text(token)
        pidfile.with_suffix(".release").unlink(missing_ok=True)
        readfd, writefd = os.pipe()
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ibhics.hypervisor.ct_runlock",
                "_hold",
                str(home),
                str(run_dir),
                token,
                str(writefd),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            pass_fds=(writefd,),
        )
        os.close(writefd)
        try:
            ready, _, _ = select.select([readfd], [], [], timeout)
            result = os.read(readfd, 1024) if ready else b""
            if result != b"OK":
                request.unlink(missing_ok=True)  # late NFS open must not become a holder
                child.kill()
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass  # an uninterruptible NFS open may exit only after reconnect
                raise RuntimeError(
                    "container is still running on another node (run lock unavailable)"
                )
        finally:
            os.close(readfd)


def release(home, run_dir=RUN_DIR, timeout=TIMEOUT):
    _, pidfile = _paths(home, run_dir)
    with pidfile.with_suffix(".guard").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        if not pidfile.exists():
            return
        data = json.loads(pidfile.read_text())
        if data["home"] != str(Path(os.path.abspath(home))):
            raise RuntimeError("Run lock path mismatch")
        pidfile.with_suffix(".release").write_text(data["token"])
        until = time.monotonic() + timeout
        while pidfile.exists():
            if not _alive(data):
                pidfile.unlink(missing_ok=True)
                break
            if time.monotonic() >= until:
                raise RuntimeError("Run lock release timed out; container remains fenced")
            time.sleep(0.05)


def _hold(home, run_dir, token, readyfd):
    home, pidfile = _paths(home, run_dir)
    request = pidfile.with_suffix(".request")
    releasefile = pidfile.with_suffix(".release")
    # lockf, not flock: NFSv4 byte-range locking must be enabled on the mount.
    fd = os.open(home / ".ibhics-run.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b", buffering=0) as stream:
        fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not request.exists() or request.read_text() != token:
            return
        data = dict(pid=os.getpid(), home=str(home), token=token, at=time.monotonic())
        try:
            while True:
                # NFS I/O errors end the holder; stalled I/O makes its heartbeat
                # stale. The agent detects either condition and requests a stop.
                # This is detection, not synchronous fencing of rootfs.img I/O.
                stream.seek(0)
                stream.write(b"1")
                os.fsync(stream.fileno())
                data["at"] = time.monotonic()
                temp = pidfile.with_suffix(".tmp")
                temp.write_text(json.dumps(data))
                temp.replace(pidfile)
                if readyfd is not None:
                    os.write(readyfd, b"OK")
                    os.close(readyfd)
                    readyfd = None
                if releasefile.exists() and releasefile.read_text() == token:
                    break
                time.sleep(0.5)
        finally:
            # Close the lock BEFORE acknowledging release by removing the pidfile.
            stream.close()
            pidfile.unlink(missing_ok=True)
            request.unlink(missing_ok=True)
            releasefile.unlink(missing_ok=True)


def main():
    try:
        action, home = sys.argv[1:3]
        if action == "_hold":
            _hold(home, sys.argv[3], sys.argv[4], int(sys.argv[5]))
        elif action == "acquire":
            acquire(home)
        elif action == "release":
            release(home)
        else:
            raise ValueError("Unknown run lock operation")
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
