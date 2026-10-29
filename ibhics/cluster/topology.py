"""Cluster shapes ibHiCS supports and the quorum rule that keeps them safe."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class NodeRole(StrEnum):
    COMPUTE = "compute"
    # A witness runs no VMs and stores no VM data; it only adds a vote so a
    # two-node cluster can tell "my peer is down" from "I am cut off".
    WITNESS = "witness"


class Topology(StrEnum):
    STANDALONE = "standalone"
    TWO_NODE_WITNESS = "two-node-witness"
    CLUSTER = "cluster"
    # Shapes that work but cannot fail over safely, e.g. two nodes without a witness.
    INCOMPLETE = "incomplete"


def classify(roles: Iterable[NodeRole]) -> Topology:
    roles = list(roles)
    compute = roles.count(NodeRole.COMPUTE)
    witness = roles.count(NodeRole.WITNESS)
    if compute == 1 and witness == 0:
        return Topology.STANDALONE
    if compute == 2 and witness == 1:
        return Topology.TWO_NODE_WITNESS
    if compute >= 3 and witness <= 1:
        return Topology.CLUSTER
    return Topology.INCOMPLETE


def has_quorum(healthy: int, total: int) -> bool:
    """Strict majority of all voting members, so two halves can never both act."""
    return total > 0 and healthy * 2 > total
