"""Device selection for the disposable Linux backup acceptance test."""

import pytest

from ops.backup.linux_smoke import qemu_io_targets, write_qemu_pattern


def test_named_legacy_backend():
    assert qemu_io_targets(
        [{"device": "drive-virtio-disk0", "inserted": {"node-name": "libvirt-1-format"}}]
    ) == ["drive-virtio-disk0", "libvirt-1-format"]


def test_modern_unnamed_backend_uses_qom_path_and_format_node():
    block = {
        "device": "",
        "qdev": "/machine/peripheral/virtio-disk0/virtio-backend",
        "inserted": {"node-name": "libvirt-1-format"},
    }
    assert qemu_io_targets([block]) == [
        "-d /machine/peripheral/virtio-disk0/virtio-backend",
        "libvirt-1-format",
    ]


def test_format_node_without_frontend_name():
    assert qemu_io_targets([{"inserted": {"node-name": "libvirt-1-format"}}]) == [
        "libvirt-1-format"
    ]


@pytest.mark.parametrize(
    "blocks",
    [
        [],
        [{"inserted": {"node-name": "a"}}, {"inserted": {"node-name": "b"}}],
        [{"device": "bad;command", "inserted": {"node-name": '"unsafe'}}],
    ],
)
def test_refuses_ambiguous_or_invalid_devices(blocks):
    with pytest.raises(RuntimeError):
        qemu_io_targets(blocks)


def test_empty_hmp_response_waits_for_readback_instead_of_trying_another_backend():
    calls = []

    def qmp(command, args):
        calls.append(args["command-line"])
        return ""

    target = write_qemu_pattern(
        qmp, ["-d /machine/peripheral/test", "libvirt-1-format"], 1, 65, 4096
    )
    assert target == "-d /machine/peripheral/test"
    assert calls == [
        'qemu-io -d /machine/peripheral/test "write -P 65 4096 4096"',
        'qemu-io -d /machine/peripheral/test "flush"',
    ]


def test_explicit_write_errors_are_not_accepted():
    def qmp(command, args):
        return "Permission conflict on test node"

    with pytest.raises(RuntimeError, match="Permission conflict"):
        write_qemu_pattern(qmp, ["node"], 0, 65, 4096)


def test_partial_write_is_not_accepted():
    def qmp(command, args):
        return "wrote 1024/4096 bytes"

    with pytest.raises(RuntimeError):
        write_qemu_pattern(qmp, ["node"], 0, 65, 4096)


def test_qemu_test_directory_is_traversable_under_restrictive_installer_umask(tmp_path):
    import os
    import stat

    from ops.backup.linux_smoke import qemu_test_directory

    previous = os.umask(0o077)
    try:
        workspace = qemu_test_directory(tmp_path / "new-test")
        scratch = qemu_test_directory(workspace / "scratch")
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o755
        assert stat.S_IMODE(scratch.stat().st_mode) == 0o755
        private = tmp_path / "evidence"
        private.mkdir(mode=0o700)
        assert stat.S_IMODE(private.stat().st_mode) == 0o700
    finally:
        os.umask(previous)


def test_qemu_test_directory_refuses_to_change_an_existing_directory(tmp_path):
    from ops.backup.linux_smoke import qemu_test_directory

    existing = tmp_path / "existing"
    existing.mkdir(mode=0o700)
    with pytest.raises(FileExistsError):
        qemu_test_directory(existing)
    assert existing.stat().st_mode & 0o777 == 0o700
