"""A running VM's NIC moves to another network through a bare interface, and is checked."""

import xml.etree.ElementTree as ET

import pytest

from ibhics.hypervisor import InvalidState
from ibhics.hypervisor.libvirt_driver import _move_live_nic

SAVED = """<interface type='network'><mac address='52:54:00:26:69:33'/>
<source network='VLAN2'/><model type='virtio'/><address type='pci' slot='0x00'/></interface>"""


class Dom:
    def __init__(self, applies=True):
        self.applies, self.sent, self.network = applies, [], "Vlan11"

    def updateDeviceFlags(self, xml, flags):  # noqa: N802 - libvirt's name
        self.sent.append(xml)
        if self.applies:
            self.network = ET.fromstring(xml).find("source").get("network")

    def XMLDesc(self, flags):  # noqa: N802
        return (
            "<domain><devices><interface type='bridge'><mac address='52:54:00:26:69:33'/>"
            f"<source network='{self.network}' bridge='ibr'/></interface></devices></domain>"
        )


def test_bare_interface_is_sent_and_checked():
    dom = Dom()
    _move_live_nic(dom, ET.fromstring(SAVED), 1)
    sent = ET.fromstring(dom.sent[0])
    assert sent.find("address") is None and sent.find("source").get("network") == "VLAN2"
    assert dom.network == "VLAN2"


def test_a_move_that_does_not_take_is_an_error():
    with pytest.raises(InvalidState, match="did not move"):
        _move_live_nic(Dom(applies=False), ET.fromstring(SAVED), 1)
