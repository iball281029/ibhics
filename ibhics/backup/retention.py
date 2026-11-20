"""Restore-point retention: recent points plus distinct weekly/monthly/yearly buckets."""

from datetime import datetime


def retained_ids(manifests, keep_last, weekly=0, monthly=0, yearly=0):
    if keep_last < 1 or any(n < 0 for n in (weekly, monthly, yearly)):
        raise ValueError("Keep at least one recent point; GFS counts must be nonnegative")
    candidates = sorted(
        manifests,
        key=lambda m: (m.get("data_created", m["created"]), m["created"], m["id"]),
        reverse=True,
    )
    points, states = [], set()
    for manifest in candidates:
        state = (manifest["vm_uuid"], manifest.get("data_created", manifest["created"]))
        if state not in states:
            points.append(manifest)
            states.add(state)
    keep = {m["id"] for m in points[:keep_last]}
    for count, bucket in (
        (weekly, lambda d: d.isocalendar()[:2]),
        (monthly, lambda d: (d.year, d.month)),
        (yearly, lambda d: d.year),
    ):
        seen = set()
        for manifest in points:
            if len(seen) >= count:
                break
            key = bucket(datetime.fromisoformat(manifest.get("data_created", manifest["created"])))
            if key not in seen:
                seen.add(key)
                keep.add(manifest["id"])
    return keep
