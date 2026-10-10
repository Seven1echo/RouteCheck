from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any

import httpx


# Mihomo's default fake-ip range is covered by 198.18.0.0/15.
FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"),)
DOH_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("阿里", "https://dns.alidns.com/resolve"),
    ("腾讯", "https://doh.pub/resolve"),
    ("Cloudflare", "https://cloudflare-dns.com/dns-query"),
)


def is_fake_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in FAKE_IP_NETWORKS)


def _unique_addresses(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


async def resolve_system(domain: str) -> list[str]:
    infos = await asyncio.to_thread(socket.getaddrinfo, domain, 443, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for item in infos:
        value = item[4][0]
        try:
            ipaddress.ip_address(value)
        except ValueError:
            continue
        addresses.append(value)
    return _unique_addresses(addresses)


def _parse_doh(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    addresses: list[str] = []
    for answer in payload.get("Answer", []) or []:
        value = answer.get("data") if isinstance(answer, dict) else None
        try:
            ipaddress.ip_address(value)
        except (TypeError, ValueError):
            continue
        if not is_fake_ip(value):
            addresses.append(value)
    return _unique_addresses(addresses)


async def resolve_doh(domain: str, endpoint: str) -> list[str]:
    headers = {"Accept": "application/dns-json"}
    async with httpx.AsyncClient(timeout=8, trust_env=False, headers=headers) as client:
        response = await client.get(endpoint, params={"name": domain, "type": "A"})
        response.raise_for_status()
        return _parse_doh(response.json())


async def resolve_domain(domain: str) -> tuple[list[str], str]:
    """Resolve through system DNS first, then DoH when it fails or returns fake-ip."""
    try:
        addresses = await resolve_system(domain)
        if addresses and not any(is_fake_ip(value) for value in addresses):
            return addresses, "system"
    except (OSError, socket.gaierror):
        pass

    for provider, endpoint in DOH_PROVIDERS:
        try:
            addresses = await resolve_doh(domain, endpoint)
        except Exception:
            continue
        if addresses:
            return addresses, f"DoH:{provider}"
    return [], "unresolved"
