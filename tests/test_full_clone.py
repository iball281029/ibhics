import errno
import os

import pytest

from ibhics.hypervisor.full_clone import try_reflink


def images(tmp_path):
    source, target = tmp_path / "source.qcow2", tmp_path / "target.qcow2"
    source.write_bytes(b"source current image")
    target.write_bytes(b"libvirt empty image")
    target.chmod(0o640)
    return source, target


def info(_):
    return {"format": "qcow2", "virtual-size": 128 * 1024**3}


def copy_ioctl(target, request, source):
    os.write(target, os.read(source, 4096))


def test_fast_clone_is_independent_preserves_permissions_and_has_no_temporary_files(tmp_path):
    source, target = images(tmp_path)
    assert try_reflink(source, target, 128 * 1024**3, inspect=info, ioctl=copy_ioctl)
    assert source.read_bytes() == target.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o640
    target.write_bytes(b"new VM writes")
    assert source.read_bytes() == b"source current image"
    source.unlink()
    assert target.read_bytes() == b"new VM writes"
    assert not list(tmp_path.glob(".ibhics-full-clone-*"))


@pytest.mark.parametrize(
    "extra",
    [
        {"backing-filename": "base.qcow2"},
        {"snapshots": [{"id": "1"}]},
        {"virtual-size": 1},
        {"format": "raw"},
    ],
)
def test_dependencies_snapshots_or_format_changes_use_normal_conversion(tmp_path, extra):
    source, target = images(tmp_path)
    assert not try_reflink(
        source,
        target,
        128 * 1024**3,
        inspect=lambda _: {**info(None), **extra},
        ioctl=lambda *a: pytest.fail("must not attempt reflink"),
    )
    assert target.read_bytes() == b"libvirt empty image"


def test_unsupported_reflink_preserves_target_for_converter_fallback(tmp_path):
    source, target = images(tmp_path)

    def unsupported(*_):
        raise OSError(errno.EOPNOTSUPP, "No clone support")

    assert not try_reflink(source, target, 128 * 1024**3, inspect=info, ioctl=unsupported)
    assert target.read_bytes() == b"libvirt empty image"
    assert not list(tmp_path.glob(".ibhics-full-clone-*"))


def test_storage_failure_is_not_silently_treated_as_unsupported(tmp_path):
    source, target = images(tmp_path)

    def failure(*_):
        raise OSError(errno.EIO, "Storage failure")

    with pytest.raises(OSError):
        try_reflink(source, target, 128 * 1024**3, inspect=info, ioctl=failure)
    assert target.read_bytes() == b"libvirt empty image"
    assert not list(tmp_path.glob(".ibhics-full-clone-*"))
