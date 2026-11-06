"""The few ESXi API (SOAP, /sdk) calls ibHiCS Migrate needs, without the vSphere SDK."""

from __future__ import annotations

import ssl
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from xml.sax.saxutils import escape

from .sources import Source

NS = {"s": "http://schemas.xmlsoap.org/soap/envelope/", "v": "urn:vim25"}
# Calls take (body, cookie) and return (status, response text, cookie).
Transport = Callable[[str, str | None], tuple[int, str, str | None]]


class EsxiApiError(RuntimeError):
    pass


def envelope(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<soapenv:Envelope xmlns:soapenv="{NS["s"]}" xmlns="urn:vim25">'
        f"<soapenv:Body>{body}</soapenv:Body></soapenv:Envelope>"
    )


def https_transport(source: Source) -> Transport:
    ctx = ssl.create_default_context()
    if not source.verify_tls:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE

    def call(body: str, cookie: str | None) -> tuple[int, str, str | None]:
        req = urllib.request.Request(
            f"https://{source.host}/sdk", data=envelope(body).encode(), method="POST"
        )
        req.add_header("Content-Type", "text/xml; charset=utf-8")
        req.add_header("SOAPAction", "urn:vim25/6.0")
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
                got = resp.headers.get("Set-Cookie", "").split(";")[0] or cookie
                return resp.status, resp.read().decode(), got
        except urllib.error.HTTPError as exc:  # SOAP faults come back as HTTP 500
            return exc.code, exc.read().decode(errors="replace"), cookie

    return call


class EsxiApi:
    def __init__(self, source: Source, transport: Transport | None = None) -> None:
        self.source = source
        self._call = transport or https_transport(source)
        self._cookie: str | None = None

    def _request(self, body: str) -> ET.Element:
        status, text, self._cookie = self._call(body, self._cookie)
        root = ET.fromstring(text)
        fault = root.find(".//s:Fault/faultstring", NS)
        if status != 200 or fault is not None:
            raise EsxiApiError(fault.text if fault is not None else f"HTTP {status}")
        return root.find("s:Body", NS)[0]

    def login(self) -> None:
        content = self._request(
            '<RetrieveServiceContent><_this type="ServiceInstance">ServiceInstance</_this>'
            "</RetrieveServiceContent>"
        )
        manager = content.find(".//v:sessionManager", NS).text
        self._request(
            f'<Login><_this type="SessionManager">{escape(manager)}</_this>'
            f"<userName>{escape(self.source.username or '')}</userName>"
            f"<password>{escape(self.source.password or '')}</password></Login>"
        )

    def service_system(self) -> str:
        found = self._request(
            '<RetrieveProperties><_this type="PropertyCollector">ha-property-collector</_this>'
            "<specSet><propSet><type>HostSystem</type>"
            "<pathSet>configManager.serviceSystem</pathSet></propSet>"
            '<objectSet><obj type="HostSystem">ha-host</obj></objectSet></specSet>'
            "</RetrieveProperties>"
        )
        val = found.find(".//v:val", NS)
        if val is None or not val.text:
            raise EsxiApiError("the ESXi host did not report its service system")
        return val.text

    def start_ssh(self) -> None:
        """Turn on the ESXi Shell's SSH service (TSM-SSH) until the next reboot."""
        self.login()
        system = self.service_system()
        self._request(
            f'<StartService><_this type="HostServiceSystem">{escape(system)}</_this>'
            "<id>TSM-SSH</id></StartService>"
        )
