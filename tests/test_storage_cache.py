import json

import pytest
from pydantic import ValidationError

from ibhics.storage_cache import CacheModeIn, cache_status, set_cache_mode


class Cache:
    def __init__(self, mode="writethrough", dirty=0, stuck=False):
        self.mode, self.dirty, self.stuck = mode, dirty, stuck
        self.policy, self.calls = "smq", []

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "lvs":
            if self.policy == "cleaner" and not self.stuck:
                self.dirty = 0
            return json.dumps(
                {
                    "report": [
                        {
                            "lv": [
                                {
                                    "lv_name": "[thin_tdata]",
                                    "segtype": "cache",
                                    "cache_mode": self.mode,
                                    "cache_policy": self.policy,
                                    "cache_dirty_blocks": str(self.dirty),
                                    "chunk_size": "1048576",
                                }
                            ]
                        }
                    ]
                }
            )
        assert args[0] == "lvchange" and args[-1] == "ibhv-test/thin_tdata"
        if "--cachepolicy" in args:
            self.policy = args[3]
        else:
            assert "--cachemode" in args
            if args[3] == "writethrough":
                assert self.dirty == 0
            self.mode = args[3]
        return ""


def test_writeback_changes_existing_cache_without_disk_preparation(tmp_path):
    run = Cache()
    result = set_cache_mode(CacheModeIn(name="test", mode="writeback"), root=tmp_path, run=run)
    assert result["mode"] == "writeback"
    assert all(args[0] in ("lvs", "lvchange") for args in run.calls)


def test_writethrough_flushes_dirty_cache_and_restores_policy(tmp_path):
    run = Cache("writeback", dirty=12)
    assert cache_status("test", run)["dirty_bytes"] == 12 * 1024**2
    result = set_cache_mode(CacheModeIn(name="test", mode="writethrough"), root=tmp_path, run=run)
    assert result["mode"] == "writethrough" and result["dirty_bytes"] == 0
    assert run.policy == "smq"


def test_flush_timeout_keeps_writeback_and_never_forces_or_detaches_cache(tmp_path):
    run = Cache("writeback", dirty=12, stuck=True)
    ticks = iter([0, 601])
    with pytest.raises(RuntimeError, match="still flushing"):
        set_cache_mode(
            CacheModeIn(name="test", mode="writethrough"),
            root=tmp_path,
            run=run,
            clock=lambda: next(ticks),
            sleep=lambda _: None,
        )
    assert run.mode == "writeback" and run.policy == "smq"
    assert not any("--cachemode" in args for args in run.calls)


def test_only_managed_storage_names_and_supported_modes_are_accepted():
    for name, mode in [("../../default", "writeback"), ("test", "passthrough")]:
        with pytest.raises(ValidationError):
            CacheModeIn(name=name, mode=mode)
    with pytest.raises(ValueError, match="no SSD cache"):
        cache_status("test", lambda _: '{"report":[{"lv":[]}]}')
