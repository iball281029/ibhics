"""Validation shared by manager and node import entry points."""

import re


def validate_mac(mac: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", mac):
        raise ValueError("MAC must use xx:xx:xx:xx:xx:xx format")
    mac = mac.lower()
    if int(mac[:2], 16) & 1 or mac == "00:00:00:00:00:00":
        raise ValueError("MAC must be a nonzero unicast address")
    return mac


def validate_nics(nics, vm, occupied=()):
    if nics is None:
        return
    if len(nics) != max(vm.nics, len(vm.networks), len(vm.macs)):
        raise ValueError("nics must have one entry per source NIC, in source order")
    seen = {m.lower() for m in occupied if m}
    for i, nic in enumerate(nics):
        mode = nic.get("mac_mode")
        if mode not in ("keep", "new", "custom") or not nic.get("network"):
            raise ValueError("each NIC needs a network and valid MAC mode")
        mac = nic.get("mac")
        if mode == "keep":
            if vm.state == "running":
                raise ValueError("Power off the source VM before keeping its MAC")
            mac = vm.macs[i] if i < len(vm.macs) else None
        elif mode == "new":
            if mac is not None:
                raise ValueError("new MAC mode does not accept an explicit MAC")
        elif not mac:
            raise ValueError("custom MAC mode requires a MAC")
        if mac:
            mac = validate_mac(mac)
            if mac in seen:
                raise ValueError("duplicate MAC address")
            seen.add(mac)
