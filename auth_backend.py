"""V380 QR login support and authorized-bootstrap validation/import."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import re
from typing import Any

import account_auth


class BootstrapValidationError(ValueError):
    def __init__(self, fields: list[str]):
        self.fields = fields
        super().__init__("Invalid bootstrap fields: " + ", ".join(fields))


class AccountBackendError(RuntimeError):
    pass


def _valid_host(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 253:
        return False
    if any(ch.isspace() or ch in "/\\:@?#" for ch in value):
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    labels = value.rstrip(".").split(".")
    return all(
        label
        and len(label) <= 63
        and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
        for label in labels
    )


def _valid_path(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("/")
        and len(value) <= 256
        and not any(ord(ch) < 32 or ch == "\\" for ch in value)
    )


def _valid_port(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def validate_bootstrap(document: Any) -> dict[str, Any]:
    """Return only the fields consumed by the receiver, never error values."""
    if not isinstance(document, dict):
        raise BootstrapValidationError(["top-level object"])

    errors: list[str] = []
    token = document.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", token):
        errors.append("token")

    device_id = document.get("device_id")
    if (
        not isinstance(device_id, int)
        or isinstance(device_id, bool)
        or not 1 <= device_id <= 0xFFFFFFFF
    ):
        errors.append("device_id")

    public_key = document.get("device_public_key")
    if isinstance(public_key, str):
        try:
            decoded_key = base64.b64decode(public_key, validate=True)
            if len(decoded_key) != 32:
                errors.append("device_public_key")
        except (binascii.Error, ValueError):
            errors.append("device_public_key")
    else:
        errors.append("device_public_key")

    signature_suffix = document.get("signature_suffix")
    if (
        not isinstance(signature_suffix, str)
        or not signature_suffix
        or len(signature_suffix) > 256
        or any(ord(ch) < 32 for ch in signature_suffix)
    ):
        errors.append("signature_suffix")

    secret_ip = document.get("secret_ip")
    if not isinstance(secret_ip, str):
        errors.append("secret_ip")
    else:
        try:
            ipaddress.ip_address(secret_ip)
        except ValueError:
            errors.append("secret_ip")

    secret_ips = document.get("secret_ips", [])
    normalized_secret_ips: list[str] = []
    if not isinstance(secret_ips, list) or len(secret_ips) > 8:
        errors.append("secret_ips")
    else:
        for address in secret_ips:
            if not isinstance(address, str):
                errors.append("secret_ips[]")
                continue
            try:
                normalized_secret_ips.append(str(ipaddress.ip_address(address)))
            except ValueError:
                errors.append("secret_ips[]")

    secret_host = document.get("secret_host", "secret.av380.net")
    if not _valid_host(secret_host):
        errors.append("secret_host")

    secret_hosts = document.get("secret_hosts", [])
    normalized_secret_hosts: list[str] = []
    if not isinstance(secret_hosts, list) or len(secret_hosts) > 8:
        errors.append("secret_hosts")
    else:
        for host in secret_hosts:
            if not _valid_host(host):
                errors.append("secret_hosts[]")
            else:
                normalized_secret_hosts.append(host)

    secret_path = document.get("secret_path", "/v1/app/exch")
    if not _valid_path(secret_path):
        errors.append("secret_path")

    call_path = document.get("call_path", "/v1/app/call")
    if not _valid_path(call_path):
        errors.append("call_path")

    call_value = document.get("call_value", 4)
    if not isinstance(call_value, int) or isinstance(call_value, bool) or not 1 <= call_value <= 0xFFFFFFFF:
        errors.append("call_value")

    dispatch_platform = document.get("dispatch_platform", 20000)
    if (
        not isinstance(dispatch_platform, int)
        or isinstance(dispatch_platform, bool)
        or not 1 <= dispatch_platform <= 0xFFFFFFFF
    ):
        errors.append("dispatch_platform")

    dispatch_salt = document.get("dispatch_salt", "hsdata2022")
    if (
        not isinstance(dispatch_salt, str)
        or not dispatch_salt
        or len(dispatch_salt) > 256
        or any(ord(ch) < 32 for ch in dispatch_salt)
    ):
        errors.append("dispatch_salt")

    dispatch_host = document.get("dispatch_host", "dispatch.av380.net")
    if not _valid_host(dispatch_host):
        errors.append("dispatch_host")

    dispatch_hosts = document.get("dispatch_hosts", [])
    normalized_dispatch_hosts: list[str] = []
    if not isinstance(dispatch_hosts, list) or len(dispatch_hosts) > 8:
        errors.append("dispatch_hosts")
    else:
        for host in dispatch_hosts:
            if not _valid_host(host):
                errors.append("dispatch_hosts[]")
            else:
                normalized_dispatch_hosts.append(host)

    dispatch_port = document.get("dispatch_port", 8001)
    if not _valid_port(dispatch_port):
        errors.append("dispatch_port")

    relays = document.get("relays")
    normalized_relays: list[dict[str, Any]] = []
    if not isinstance(relays, list) or not 1 <= len(relays) <= 12:
        errors.append("relays")
    else:
        for relay in relays:
            if not isinstance(relay, dict) or not _valid_host(relay.get("ip")):
                errors.append("relays[].ip")
                continue
            tp = relay.get("tp", {})
            if not isinstance(tp, dict):
                errors.append("relays[].tp")
                continue
            call_port = tp.get("call_port", 80)
            trans_port = tp.get("trans_port", 32100)
            if not _valid_port(call_port):
                errors.append("relays[].tp.call_port")
            if not _valid_port(trans_port):
                errors.append("relays[].tp.trans_port")
            if _valid_port(call_port) and _valid_port(trans_port):
                normalized_relays.append(
                    {"ip": relay["ip"], "tp": {"call_port": call_port, "trans_port": trans_port}}
                )

    headers = document.get("secret_headers", {})
    normalized_headers: dict[str, str] = {}
    if not isinstance(headers, dict):
        errors.append("secret_headers")
    elif "user-agent" in headers:
        user_agent = headers["user-agent"]
        if (
            not isinstance(user_agent, str)
            or not user_agent
            or len(user_agent) > 256
            or any(ord(ch) < 32 for ch in user_agent)
        ):
            errors.append("secret_headers.user-agent")
        else:
            normalized_headers["user-agent"] = user_agent

    if errors:
        raise BootstrapValidationError(list(dict.fromkeys(errors)))

    return {
        "token": token,
        "device_id": device_id,
        "device_public_key": public_key,
        "signature_suffix": signature_suffix,
        "secret_host": secret_host,
        "secret_hosts": normalized_secret_hosts,
        "secret_path": secret_path,
        "secret_ip": secret_ip,
        "secret_ips": list(dict.fromkeys(normalized_secret_ips)),
        "call_path": call_path,
        "call_value": call_value,
        "dispatch_platform": dispatch_platform,
        "dispatch_salt": dispatch_salt,
        "dispatch_host": dispatch_host,
        "dispatch_hosts": list(dict.fromkeys(normalized_dispatch_hosts)),
        "dispatch_port": dispatch_port,
        "relays": normalized_relays,
        "secret_headers": normalized_headers,
    }


class AuthorizationBackend:
    """V380 QR-login adapter plus the authorized-bootstrap import fallback."""

    def import_bootstrap(self, raw: bytes) -> dict[str, Any]:
        if len(raw) > 64 * 1024:
            raise BootstrapValidationError(["file size (maximum 64 KiB)"])
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BootstrapValidationError(["valid UTF-8 JSON"]) from None
        return validate_bootstrap(document)

    def start_qr(self) -> dict[str, Any]:
        try:
            return account_auth.start_qr()
        except account_auth.AccountError as exc:
            raise AccountBackendError(str(exc)) from None

    def poll_qr(self, session: dict[str, Any]) -> dict[str, Any]:
        try:
            return account_auth.poll_qr(session)
        except account_auth.AccountError as exc:
            raise AccountBackendError(str(exc)) from None

    def list_cameras(self, token: str) -> list[dict[str, Any]]:
        try:
            return account_auth.list_cameras(token)
        except account_auth.AccountError as exc:
            raise AccountBackendError(str(exc)) from None

    def build_camera_credentials(self, token: str, camera: dict[str, Any]) -> dict[str, Any]:
        try:
            return account_auth.build_camera_credentials(token, camera)
        except account_auth.AccountError as exc:
            raise AccountBackendError(str(exc)) from None
