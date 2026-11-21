from ibhics.hypervisor.host import best_serial
from ibhics.manager.settings import cluster_key


def test_nutanix_placeholder_uses_bmc_product_serial_and_shows_board():
    fru = " Board Serial : ZM185S038512\n Product Serial        : S318360X8717169\n"
    got = best_serial("Refer to PRISM", "ZM185S038512", "ZM185S038512", fru)
    assert got == "S318360X8717169 · Board ZM185S038512"


def test_real_system_serial_kept():
    assert best_serial("J30ABCDE", "J30ABCDE", "", "") == "J30ABCDE"


def test_cluster_id_forms_match():
    uuid = "eb3ddd52-df98-47dc-8acd-71ea7e0e30d3"
    assert cluster_key("IBHX-EB3D-DD52-DF98-47DC-8ACD-71EA-7E0E-30D3") == cluster_key(uuid)


def test_immutability_days_editable(tmp_path):
    import pytest

    from ibhics.backup.targets import BackupTarget
    from ibhics.manager.backups import BackupManager

    assert hasattr(BackupManager, "set_immutability")
    with pytest.raises(ValueError):
        BackupManager.set_immutability(object.__new__(BackupManager), "x", -1)
    assert BackupTarget.__dataclass_fields__["immutability_days"].default == 0
