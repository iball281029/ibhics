"""Small vSphere SOAP CBT client using the existing httpx dependency.

Only direct ESXi is supported: SSH reads frozen base flat extents while the owned
snapshot exists. Never use a numeric vim-cmd snapshot id as a SOAP snapshot MOR.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET

import httpx

SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
VIM = "urn:vim25"
XSI = "http://www.w3.org/2001/XMLSchema-instance"


def element(parent, name, text=None, **attrs):
    child = ET.SubElement(parent, name, attrs)
    if text is not None:
        child.text = str(text)
    return child


class CBTUnavailable(RuntimeError):
    """Diagnostics are fixed strings; never include server responses or credentials."""


class VSphereCBT:
    def __init__(self, source, http=None):
        self.http = http or httpx.Client(
            base_url=f"https://{source.host}",
            verify=source.verify_tls,
            timeout=15,
            follow_redirects=False,
        )
        self.collector = self.session = None

    def call(self, method, kind, ref, build=None):
        envelope = ET.Element(f"{{{SOAP}}}Envelope")
        body = element(envelope, f"{{{SOAP}}}Body")
        operation = element(body, method, xmlns=VIM)
        element(operation, "_this", ref, type=kind)
        if build:
            build(operation)
        try:
            response = self.http.post(
                "/sdk",
                content=ET.tostring(envelope, encoding="utf-8"),
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": '"urn:vim25/6.5"',
                },
            )
            root = ET.fromstring(response.content)
            for node in root.iter():
                node.tag = node.tag.rsplit("}", 1)[-1]
            if response.status_code != 200 or root.find(".//Fault") is not None:
                raise CBTUnavailable("vSphere CBT API refused the operation")
            return root
        except (httpx.HTTPError, ET.ParseError):
            raise CBTUnavailable("vSphere CBT API is unavailable") from None

    def login(self):
        content = self.call("RetrieveServiceContent", "ServiceInstance", "ServiceInstance")
        self.collector = content.findtext(".//propertyCollector")
        self.session = content.findtext(".//sessionManager")
        if not self.collector or not self.session:
            raise CBTUnavailable("vSphere service content is incomplete")
        # The session cookie is kept in httpx memory, never in a manifest/job.
        source = self.source
        self.call(
            "Login",
            "SessionManager",
            self.session,
            lambda op: (
                element(op, "userName", source.username),
                element(op, "password", source.password),
            ),
        )

    def connect(self, source):
        self.source = source
        self.login()

    def property(self, kind, ref, path):
        def build(op):
            spec = element(op, "specSet")
            prop = element(spec, "propSet")
            element(prop, "type", kind)
            element(prop, "pathSet", path)
            obj = element(spec, "objectSet")
            element(obj, "obj", ref, type=kind)
            element(op, "options")

        root = self.call("RetrievePropertiesEx", "PropertyCollector", self.collector, build)
        for prop in root.findall(".//propSet"):
            if prop.findtext("name") == path:
                value = prop.find("val")
                if value is not None:
                    return value
        raise CBTUnavailable("vSphere CBT property is unavailable")

    def enable(self, vm_ref):
        if (
            self.property("VirtualMachine", vm_ref, "capability.changeTrackingSupported").text
            != "true"
        ):
            raise CBTUnavailable("CBT is not supported by this ESXi VM")
        if self.property("VirtualMachine", vm_ref, "config.changeTrackingEnabled").text == "true":
            return

        def build(op):
            element(element(op, "spec"), "changeTrackingEnabled", "true")

        task = self.call("ReconfigVM_Task", "VirtualMachine", vm_ref, build).findtext(
            ".//returnval"
        )
        if not task:
            raise CBTUnavailable("CBT configuration task was not returned")
        deadline = time.monotonic() + 45
        while True:
            state = self.property("Task", task, "info.state").text
            if state == "success":
                return
            if state == "error" or time.monotonic() >= deadline:
                raise CBTUnavailable(
                    "CBT configuration failed or is still pending; using hash-delta"
                )
            time.sleep(1)

    def snapshot_disks(self, vm_ref, name):
        tree = self.property("VirtualMachine", vm_ref, "snapshot")
        current = tree.findtext("currentSnapshot")
        owned = [
            entry.findtext("snapshot") for entry in tree.iter() if entry.findtext("name") == name
        ]
        if not current or current not in owned:
            raise CBTUnavailable("CBT snapshot ownership could not be verified")
        hardware = self.property("VirtualMachineSnapshot", current, "config.hardware.device")
        disks = {}
        for device in hardware:
            if device.get(f"{{{XSI}}}type", "").split(":")[-1] != "VirtualDisk":
                continue
            backing = device.find("backing")
            if backing is None or backing.get(f"{{{XSI}}}type", "").split(":")[-1] != (
                "VirtualDiskFlatVer2BackingInfo"
            ):
                raise CBTUnavailable("CBT requires supported flat VMDK backing")
            filename = backing.findtext("fileName")
            change_id = backing.findtext("changeId")
            key = device.findtext("key")
            size = int(
                device.findtext("capacityInBytes")
                or int(device.findtext("capacityInKB", "0")) * 1024
            )
            if not filename or not key or not change_id or change_id == "*" or size <= 0:
                raise CBTUnavailable("CBT changeId is not ready; establishing a full baseline")
            if filename in disks:
                raise CBTUnavailable("CBT disk identity is ambiguous")
            disks[filename] = {"key": int(key), "change_id": change_id, "size": size}
        return current, disks

    def changed(self, vm_ref, snapshot, key, size, change_id):
        ranges, offset = [], 0
        while offset < size:

            def build(op, start_offset=offset):
                element(op, "snapshot", snapshot, type="VirtualMachineSnapshot")
                element(op, "deviceKey", key)
                element(op, "startOffset", start_offset)
                element(op, "changeId", change_id)

            info = self.call("QueryChangedDiskAreas", "VirtualMachine", vm_ref, build)
            value = info.find(".//returnval")
            if value is None:
                raise CBTUnavailable("CBT returned no changed disk information")
            start, length = (
                int(value.findtext("startOffset", "-1")),
                int(value.findtext("length", "0")),
            )
            if start != offset or length <= 0 or start + length > size:
                raise CBTUnavailable("CBT returned incomplete disk coverage")
            for area in value.findall("changedArea"):
                begin = int(area.findtext("start", "-1"))
                count = int(area.findtext("length", "0"))
                if begin < start or count <= 0 or begin + count > start + length:
                    raise CBTUnavailable("CBT returned an invalid changed range")
                ranges.append((begin, count))
            offset = start + length
        return ranges

    def close(self):
        try:
            if self.session:
                self.call("Logout", "SessionManager", self.session)
        finally:
            self.http.close()
