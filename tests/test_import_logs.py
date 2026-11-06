"""Import log parsing, sampling and complete-file retrieval."""

import io
import subprocess
from pathlib import Path

import pytest

from ibhics.hypervisor.fake import FakeDriver
from ibhics.importer.convert import V2VConverter
from ibhics.importer.logs import ProgressSampler, clean_v2v_log, parse_v2v_log
from ibhics.importer.node import NodeImport

SAMPLE = (Path(__file__).parent / "fixtures/v2v-example.txt").read_text()


def test_example_summary():
    data = parse_v2v_log(SAMPLE)
    assert len(data["steps"]) == 15
    assert data["steps"][4] == {
        "t": 30.3,
        "text": (
            "Converting AlmaLinux release 8.8 (Sapphire Caracal) (redhat-based8.8) to run on KVM"
        ),
        "kind": "convert",
    }
    assert data["os"] == "AlmaLinux release 8.8 (Sapphire Caracal) (redhat-based8.8)"
    assert data["disks"] == "1/1"
    assert data["progress"] is None  # Copy finished; never attach its percentage to Finishing off.
    assert len(data["warnings"]) == 2
    assert len(data["notes"]) == 4
    assert not data["errors"]
    assert "47%" not in clean_v2v_log(SAMPLE)
    assert "****---]" not in clean_v2v_log(SAMPLE)


@pytest.mark.parametrize("spinner", ["▖", "▗", "▘", "▝", "|", "/", "-", "\\", ""])
def test_progress_cr_fragments_and_disk_reset(spinner):
    text = f"[ 95.5] Copying disk 1/2\r{spinner} 47% [*****-----]\r\n****---]\n"
    data = parse_v2v_log(text)
    assert data["progress"] == {"percent": 47, "step": "Copying disk 1/2"}
    assert data["notes"] == []
    assert clean_v2v_log(text) == "[ 95.5] Copying disk 1/2"
    assert parse_v2v_log(text + "[ 110] Copying disk 2/2\n")["progress"] is None
    assert parse_v2v_log("")["steps"] == []


def test_errors_and_multiline_diagnostics():
    text = """virt-v2v: warning: bootloader needs attention
  please check Grub2

virt-v2v: error: source VM is running
shut it down before conversion

Traceback (most recent call last):
  File "import.py", line 4
ValueError: bad disk
ibHiCS: import failed
▘ 99% [*****-]
***--]
"""
    data = parse_v2v_log(text)
    assert len(data["warnings"]) == 2
    assert "shut it down before conversion" in data["errors"]
    assert '  File "import.py", line 4' in data["errors"]
    assert "ValueError: bad disk" in data["errors"]
    assert "ibHiCS: import failed" in data["errors"]
    assert not data["notes"]


def test_sampling_percent_time_and_stage_reset():
    sample = ProgressSampler()
    assert sample.keep("[0] Copying disk 1/2", 0)
    assert sample.keep("| 0% [----]", 0)
    assert not sample.keep("/ 4% [*---]", 1)
    assert sample.keep("- 5% [*---]", 2)
    assert not sample.keep("▖ 6% [*---]", 11)
    assert sample.keep("▗ 6% [*---]", 12)
    assert not sample.keep("****---]", 13)
    assert sample.keep("virt-v2v: warning: keep this", 13)
    assert sample.keep("[20] Copying disk 2/2", 20)
    assert sample.keep("| 0% [----]", 20)


def test_streaming_and_buffered_writer_normalize_progress(tmp_path, monkeypatch):
    output = (
        "[0] Copying disk 1/1\r\n"
        + "".join(f"▖ {p}% [***---]\r" for p in range(101))
        + "****---]\r\nvirt-v2v: warning: preserved\n[12] Finishing off\n"
    )

    class Process:
        stdout = io.StringIO(output, newline=None)
        returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr("ibhics.importer.convert.subprocess.Popen", lambda *a, **kw: Process())
    monkeypatch.setattr("ibhics.importer.convert.time.monotonic", lambda: 0)
    converter = V2VConverter(log_dir=tmp_path)
    events = []
    result, path = converter._run_streaming([], {}, "demo", lambda *args: events.append(args))
    saved = path.read_text()
    assert saved.count("%") == 21
    assert "\r" not in saved and "****---]" not in saved
    assert "virt-v2v: warning: preserved" in saved
    assert result.stdout == saved
    assert events[-1] == ("Finishing off", 98)
    buffered = converter._keep_log("buffered", subprocess.CompletedProcess([], 0, output, ""))
    assert buffered.read_text() == saved


def test_node_reads_complete_log_with_optional_tail(tmp_path):
    text = SAMPLE + ("extra diagnostic\n" * 10000)
    (tmp_path / "demo.log").write_text(text)
    importer = NodeImport(FakeDriver(), converter=V2VConverter(log_dir=tmp_path))
    assert importer.log("demo") == text
    assert len(importer.log("demo", max_bytes=100).encode()) == 100
    assert importer.log("missing") == ""


def test_real_virt_v2v_block_spinner_is_progress_not_notes():
    # Lines from a real ESXi import: virt-v2v spins through block elements (U+2580-259F).
    text = (Path(__file__).parent / "fixtures" / "virt-v2v-real-blocks.log").read_text()
    parsed = parse_v2v_log(text)
    assert parsed["notes"] == [
        "virt-v2v: The QEMU Guest Agent will be installed for this guest at first boot.",
        "virt-v2v: This guest has virtio drivers installed.",
    ]
    assert len(parsed["steps"]) == 15 and parsed["steps"][-1]["text"] == "Finishing off"
    assert len(parsed["warnings"]) == 2 and not parsed["errors"]
    copying = parse_v2v_log("[  95.5] Copying disk 1/1\n\u2590  47% [*****-----]\n")
    assert copying["progress"]["percent"] == 47
