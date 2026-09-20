from __future__ import annotations

import ipaddress
from typing import Any

import httpx

from . import runtime_config
from .config import settings


def headers() -> dict[str, str]:
    secret = runtime_config.mihomo_secret()
    return {"Authorization": f"Bearer {secret}"} if secret else {}


def base_url() -> str:
    return runtime_config.mihomo_base_url()


def normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower().rstrip(".")
    if value.startswith("[") and "]" in value:
        value = value[1:value.index("]")]
    if value.count(":") == 1:
        host, _, port = value.rpartition(":")
        if port.isdigit():
            value = host
    try:
        ipaddress.ip_address(value)
        return None
    except ValueError:
        pass
    if len(value) > 253 or " " in value or "/" in value:
        return None
    return value


def extract_host(connection: dict[str, Any]) -> str | None:
    metadata = connection.get("metadata") or {}
    for key in ("host", "destinationIP", "destinationAddress", "remoteDestination"):
        host = normalize_domain(str(metadata.get(key, "")))
        if host:
            return host
    return None


def is_leak(connection: dict[str, Any]) -> bool:
    chains = [str(item).strip().lower() for item in (connection.get("chains") or [])]
    if not chains or all(item == "direct" for item in chains):
        return False
    if runtime_config.capture_mode() == "all_proxy":
        return True
    rule = str(connection.get("rule", "")).lower()
    payload = str(connection.get("rulePayload", "")).lower()
    return rule in settings.leak_rule_payloads or payload in settings.leak_rule_payloads


async def fetch_connections() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=8, headers=headers()) as client:
        response = await client.get(f"{base_url()}/connections")
        response.raise_for_status()
        payload = response.json()
    return payload.get("connections", [])


async def query_dns(domain: str) -> list[str]:
    async with httpx.AsyncClient(timeout=8, headers=headers()) as client:
        response = await client.get(f"{base_url()}/dns/query", params={"name": domain, "type": "A"})
        response.raise_for_status()
        data = response.json()
    addresses: list[str] = []
    for answer in data.get("Answer", []) or []:
        value = answer.get("data")
        try:
            ipaddress.ip_address(value)
            addresses.append(value)
        except (ValueError, TypeError):
            continue
    return addresses


async def controller_status() -> dict[str, Any]:
    """Probe the configured controller; used by the settings page test button."""
    url = base_url()
    result: dict[str, Any] = {
        "ok": False,
        "url": url,
        "version": "",
        "connections": 0,
        "dns_available": False,
        "message": "",
    }
    try:
        async with httpx.AsyncClient(timeout=6, headers=headers()) as client:
            version_response = await client.get(f"{url}/version")
            version_response.raise_for_status()
            version_payload = version_response.json()
            connections_response = await client.get(f"{url}/connections")
            connections_response.raise_for_status()
            connections_payload = connections_response.json()
            dns_response = await client.get(f"{url}/dns/query", params={"name": "example.com", "type": "A"})
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        result["message"] = "密钥不正确或接口被拒绝（HTTP " + str(code) + "）" if code in (401, 403) else f"控制器返回 HTTP {code}"
        return result
    except Exception as exc:
        result["message"] = f"无法连接控制器：{type(exc).__name__}"
        return result
    result["ok"] = True
    result["version"] = str(version_payload.get("version") or version_payload.get("meta") or "")
    result["connections"] = len(connections_payload.get("connections") or [])
    result["dns_available"] = dns_response.status_code < 400
    result["message"] = "连接正常"
    return result
