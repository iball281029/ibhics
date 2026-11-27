"""Native immutable capabilities must fail soft without weakening retention metadata."""

import errno

from ibhics.backup.store import DirStore


def test_flags_preserve_other_bits_and_clear_only_immutable(tmp_path, monkeypatch):
    store = DirStore(tmp_path)
    store.put("chunks/aa/test", b"data")
    state = [0x80]

    def ioctl(fd, command, flags, mutate):
        if command == 0x80086601:
            flags[0] = state[0]
        else:
            state[0] = flags[0]
        return 0

    monkeypatch.setattr("ibhics.backup.store.fcntl.ioctl", ioctl)
    assert store.protect("chunks/aa/test", None) and state[0] == 0x90
    store.release("chunks/aa/test")
    assert state[0] == 0x80


def test_unsupported_filesystem_probes_once_but_permission_failure_retries(tmp_path, monkeypatch):
    store = DirStore(tmp_path)
    store.put("chunks/aa/first", b"a")
    store.put("chunks/bb/second", b"b")
    calls = []

    def denied(*args):
        calls.append(True)
        raise OSError(errno.EPERM, "permission denied")

    monkeypatch.setattr("ibhics.backup.store.fcntl.ioctl", denied)
    assert not store.protect("chunks/aa/first", None)
    assert not store.protect("chunks/bb/second", None)
    assert len(calls) == 2

    def unsupported(*args):
        calls.append(True)
        raise OSError(errno.ENOTTY, "unsupported")

    monkeypatch.setattr("ibhics.backup.store.fcntl.ioctl", unsupported)
    assert not store.protect("chunks/aa/first", None)
    assert not store.protect("chunks/bb/second", None)
    assert len(calls) == 3
    assert not store.protect("chunks/aa/missing", None)
