"""Live VM load from libvirt's bulk stats (virConnectGetAllDomainStats).

libvirt reports counters (CPU nanoseconds, bytes read and written); a rate needs two
samples, so the driver keeps the previous one per VM and turns the difference into
VMStats here."""

from __future__ import annotations

from dataclasses import dataclass

from .base import VMStats


@dataclass
class Sample:
    at: float  # monotonic seconds
    cpu_ns: int
    vcpus: int
    rd: int
    wr: int
    rx: int
    tx: int
    mem_used_mb: int | None


def _total(raw: dict, kind: str, field: str) -> int:
    return sum(
        int(raw.get(f"{kind}.{i}.{field}", 0)) for i in range(int(raw.get(f"{kind}.count", 0)))
    )


def memory_used_mb(raw: dict) -> int | None:
    """What the guest really uses, from the balloon driver's report. Without the
    driver (or before its first report) libvirt has only balloon.current: unknown."""
    available = raw.get("balloon.available")
    if available is None:
        return None
    free = raw.get("balloon.usable", raw.get("balloon.unused"))
    if free is None:
        return None
    return max(0, int(available) - int(free)) // 1024


def sample(raw: dict, at: float) -> Sample:
    return Sample(
        at=at,
        cpu_ns=int(raw.get("cpu.time", 0)),
        vcpus=max(1, int(raw.get("vcpu.current", 1))),
        rd=_total(raw, "block", "rd.bytes"),
        wr=_total(raw, "block", "wr.bytes"),
        rx=_total(raw, "net", "rx.bytes"),
        tx=_total(raw, "net", "tx.bytes"),
        mem_used_mb=memory_used_mb(raw),
    )


def rates(prev: Sample | None, cur: Sample) -> VMStats:
    stats = VMStats(mem_used_mb=cur.mem_used_mb)
    if prev is None or cur.at <= prev.at:
        return stats
    dt = cur.at - prev.at

    def per_s(a: int, b: int) -> float:
        return max(0.0, (b - a) / dt)  # a counter goes back to 0 when the VM restarts

    cpu = (cur.cpu_ns - prev.cpu_ns) / (dt * 1e9 * cur.vcpus) * 100
    stats.cpu_pct = round(min(100.0, max(0.0, cpu)), 1)
    stats.disk_read_bps = per_s(prev.rd, cur.rd)
    stats.disk_write_bps = per_s(prev.wr, cur.wr)
    stats.net_rx_bps = per_s(prev.rx, cur.rx)
    stats.net_tx_bps = per_s(prev.tx, cur.tx)
    return stats
