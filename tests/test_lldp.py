import struct

from ibhics.hypervisor.lldp import parse


def tlv(kind, value):
    return struct.pack("!H", (kind << 9) | len(value)) + value


def test_parse_lldp_frame():
    frame = b"\x01\x80\xc2\x00\x00\x0e" + b"\x00" * 6 + b"\x88\xcc"
    frame += tlv(1, b"\x04" + bytes([0, 1, 2, 3, 4, 5]))
    frame += tlv(2, b"\x05Gi1/0/19")
    frame += tlv(4, b"GigabitEthernet1/0/19")
    frame += tlv(5, b"core-sw1")
    frame += tlv(6, b"Cisco IOS Software, C2960X")
    frame += tlv(8, bytes([5, 1, 10, 0, 0, 254, 2, 0, 0, 0, 1, 0]))
    frame += tlv(0, b"")
    info = parse(frame)
    assert info["port_id"] == "Gi1/0/19" and info["system_name"] == "core-sw1"
    assert info["chassis_id"] == "00:01:02:03:04:05" and info["management"] == "10.0.0.254"
    assert info["system_desc"].startswith("Cisco")
