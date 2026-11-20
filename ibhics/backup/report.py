"""Per-job savings: zero blocks and CBT-skipped data are not counted as dedup."""

from datetime import datetime


def backup_report(job):
    info = job.report
    if job.state != "done" or not info:
        raise ValueError("A completed backup job with collected metrics is required")
    new = info.get("new_data_bytes", 0)
    saved = info.get("dedup_saved_bytes", 0)
    stored = info.get("stored_bytes", 0)
    before = new + saved
    return {
        "job_id": job.id,
        "job_kind": job.kind,
        "state": job.state,
        "vm": job.vm,
        "node": job.node,
        "repository": job.target,
        "policy": job.policy,
        "backup_id": job.result,
        "source_backup_id": info.get("id"),
        "synthetic_full_id": info.get("synthetic_full_id"),
        "backup_method": info.get("kind", "full"),
        "started": job.created,
        "finished": job.finished,
        "duration_seconds": max(
            0,
            (
                datetime.fromisoformat(job.finished) - datetime.fromisoformat(job.created)
            ).total_seconds(),
        ),
        "logical_size_bytes": info.get("size_bytes", 0),
        "source_read_bytes": info.get("read_bytes", 0),
        "nonzero_input_bytes": before,
        "dedup_saved_bytes": saved,
        "new_data_bytes": new,
        "stored_bytes": stored,
        "dedup_ratio": round(before / new, 4) if new else None,
        "all_data_reused": new == 0 and saved > 0,
        "compression_ratio": round(new / stored, 4) if stored and new else None,
        "combined_ratio": round(before / stored, 4) if stored and before else None,
        "compression_codec": info.get("compression", "unknown"),
        "note": info.get("note", ""),
        "metric_basis": "Dedup: nonzero processed bytes / new unique bytes. "
        "Compression: new unique bytes / added compressed payload. "
        "Zero blocks and CBT-skipped blocks are excluded. "
        "Metadata, filesystem overhead and replication are excluded.",
    }
