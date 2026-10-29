import pytest

from ibhics.cluster import NodeRole, Topology, classify, has_quorum

C, W = NodeRole.COMPUTE, NodeRole.WITNESS


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        ([C], Topology.STANDALONE),
        ([C, C, W], Topology.TWO_NODE_WITNESS),
        ([C, C, C], Topology.CLUSTER),
        ([C, C, C, C, W], Topology.CLUSTER),
        ([C, C], Topology.INCOMPLETE),
        ([C, W], Topology.INCOMPLETE),
        ([C, C, W, W], Topology.INCOMPLETE),
        ([], Topology.INCOMPLETE),
    ],
)
def test_classify(roles, expected):
    assert classify(roles) == expected


@pytest.mark.parametrize(
    ("healthy", "total", "expected"),
    [
        (1, 1, True),  # standalone
        (3, 3, True),
        (2, 3, True),  # two nodes + witness survives losing any one member
        (1, 3, False),
        (1, 2, False),  # two nodes without a witness cannot tell who should win
        (2, 4, False),
        (0, 0, False),
    ],
)
def test_quorum(healthy, total, expected):
    assert has_quorum(healthy, total) is expected
