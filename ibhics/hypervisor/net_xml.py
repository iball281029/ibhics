"""VM networks on VLANs of the node's LAN bridge.

The LAN bridge br0 carries the management address and the switch port's untagged traffic
(libvirt's "default" network). A VLAN network adds br0.<id>, which takes the frames tagged
<id> off br0, and a bridge ibr<id> for VMs on top of it. Their ifupdown stanza keeps them
across reboots.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

LAN_BRIDGE = "br0"
IFACES_DIR = "/etc/network/interfaces.d"
VLAN_BRIDGE = re.compile(r"^ibr(\d{1,4})$")


def vlan_bridge(vlan: int) -> str:
    return f"ibr{vlan}"


def vlan_iface_file(vlan: int) -> str:
    return f"{IFACES_DIR}/ibhics-vlan{vlan}"


def render_vlan_iface(vlan: int, parent: str = LAN_BRIDGE) -> str:
    bridge, link = vlan_bridge(vlan), f"{parent}.{vlan}"
    return (
        f"# Written by ibHiCS: VM network on VLAN {vlan} of {parent}.\n"
        f"auto {bridge}\n"
        f"iface {bridge} inet manual\n"
        f"    pre-up ip link add {bridge} type bridge stp_state 0 forward_delay 0 || true\n"
        f"    pre-up ip link add link {parent} name {link} type vlan id {vlan} || true\n"
        f"    pre-up ip link set {link} master {bridge}\n"
        f"    pre-up ip link set {link} up\n"
        f"    post-down ip link del {link} || true\n"
        f"    post-down ip link del {bridge} || true\n"
    )


def build_network_xml(name: str, bridge: str) -> str:
    net = ET.Element("network")
    ET.SubElement(net, "name").text = name
    ET.SubElement(net, "forward", mode="bridge")
    ET.SubElement(net, "bridge", name=bridge)
    return ET.tostring(net, encoding="unicode")


def vlan_of(bridge: str) -> int | None:
    m = VLAN_BRIDGE.match(bridge)
    return int(m[1]) if m else None
