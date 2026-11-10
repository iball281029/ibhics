from types import SimpleNamespace

from ibhics.manager.usage import unique_storage_pools


def test_shared_exports_count_once_but_local_defaults_remain_separate():
    def pool(kind, source, capacity, active=True):
        return SimpleNamespace(type=kind, source=source, capacity_gb=capacity, active=active)

    mounts = [
        pool("dir", "/default", 20),
        pool("dir", "/default", 20),
        pool("nfs", "vip:/shared", 6703, False),
        pool("nfs", "vip:/shared", 6703),
    ]
    unique = unique_storage_pools(mounts)
    assert len(unique) == 3
    assert sum(p.capacity_gb for p in unique) == 6743
    assert unique[-1] is mounts[-1]
    assert len(mounts) == 4
