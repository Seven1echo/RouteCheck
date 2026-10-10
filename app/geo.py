from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from . import runtime_config
from .config import settings
from .countries import flag_emoji, country_name_zh


_readers: dict[str, Any] = {}
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
CACHE_TTL_SECONDS = 6 * 3600


def flag_for(code: str | None) -> str:
    return flag_emoji(code)


def clear_reader_cache() -> None:
    _readers.clear()
    _cache.clear()


def _reader(path: str) -> Any | None:
    if not path:
        return None
    if path in _readers:
        return _readers[path]
    if not Path(path).exists():
        return None
    try:
        import geoip2.database

        reader = geoip2.database.Reader(path)
    except Exception:
        reader = None
    _readers[path] = reader
    return reader


def _zh(names: Any, fallback: str | None = None) -> str:
    if not isinstance(names, dict):
        return fallback or ""
    for key in ("zh-CN", "zh", "en"):
        value = names.get(key)
        if value:
            return str(value)
    return fallback or ""


def _from_city_db(ip: str) -> dict[str, Any] | None:
    reader = _reader(runtime_config.geoip_path("city"))
    if reader is None:
        return None
    try:
        record = reader.city(ip)
    except Exception:
        return None
    country = record.country.iso_code
    city = _zh(record.city.names, record.city.name)
    province = ""
    try:
        subdivisions = getattr(record, "subdivisions", None)
        most_specific = getattr(subdivisions, "most_specific", None) if subdivisions else None
        if most_specific is not None:
            province = _zh(most_specific.names, most_specific.name)
    except Exception:
        province = ""
    return {
        "country": (country or "").upper(),
        "country_name": _zh(record.country.names, record.country.name),
        "province": province,
        "city": city,
        "source": "city_db",
    }


def _from_country_db(ip: str) -> dict[str, Any] | None:
    reader = _reader(runtime_config.geoip_path("country"))
    if reader is None:
        return None
    try:
        record = reader.country(ip)
    except Exception:
        return None
    country = record.country.iso_code
    return {
        "country": (country or "").upper(),
        "country_name": _zh(record.country.names, record.country.name),
        "province": "",
        "city": "",
        "source": "country_db",
    }


def _parse_online(payload: Any, ip: str = "", source: str = "ip-api.com") -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("status") == "fail" or payload.get("success") is False:
        return None
    code = payload.get("countryCode") or payload.get("country_code") or payload.get("country_code2") or ""
    code = str(code).strip().upper()
    if len(code) != 2:
        return None
    return {
        "ip": str(payload.get("query") or payload.get("ip") or ip),
        "country": code if len(code) == 2 else "",
        "country_name": str(payload.get("country") or payload.get("country_name") or ""),
        "province": str(payload.get("regionName") or payload.get("region") or payload.get("province") or ""),
        "city": str(payload.get("city") or payload.get("city_name") or ""),
        "source": source,
    }


async def _ipwho_lookup(ip: str) -> dict[str, Any] | None:
    try:
        url = settings.geo_fallback_url.format(ip=ip)
        async with httpx.AsyncClient(timeout=settings.geo_lookup_timeout, trust_env=False) as client:
            response = await client.get(url, headers={"User-Agent": "routecheck/1.0"})
            response.raise_for_status()
        return _parse_online(response.json(), ip, "ipwho.is")
    except Exception:
        return None


async def _ip_api_batch(ips: list[str]) -> dict[str, dict[str, Any]]:
    if not ips or not runtime_config.online_geo_lookup() or not settings.geo_lookup_url:
        return {}
    result: dict[str, dict[str, Any]] = {}
    configured_url = settings.geo_lookup_url.rstrip("/")
    # Accept the pre-batch setting format too, so an existing .env keeps working.
    if "/json" in configured_url:
        configured_url = configured_url.split("/json", 1)[0]
    url = configured_url + "/batch"
    fields = "status,message,country,countryCode,regionName,city,query"
    try:
        async with httpx.AsyncClient(timeout=settings.geo_lookup_timeout, trust_env=False) as client:
            for start in range(0, len(ips), 100):
                chunk = ips[start : start + 100]
                response = await client.post(
                    url,
                    params={"lang": "zh-CN", "fields": fields},
                    json=chunk,
                    headers={"User-Agent": "routecheck/1.0"},
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    continue
                for item, requested_ip in zip(payload, chunk):
                    parsed = _parse_online(item, requested_ip, "ip-api.com")
                    if parsed:
                        result[requested_ip] = parsed
    except Exception:
        # The individual ipwho.is fallback below handles the whole batch failure.
        return result
    return result


def _empty_info(ip: str, source: str = "none") -> dict[str, Any]:
    return {
        "ip": ip,
        "country": "",
        "country_name": "",
        "province": "",
        "city": "",
        "source": source,
    }


async def lookup_geo_batch(ips: list[str]) -> list[dict[str, Any]]:
    ordered = list(dict.fromkeys(ips))
    now = time.time()
    result: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for ip in ordered:
        cached = _cache.get(ip)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            result[ip] = cached[1]
        else:
            missing.append(ip)

    online = await _ip_api_batch(missing)
    unresolved = [ip for ip in missing if ip not in online]
    if runtime_config.online_geo_lookup() and unresolved:
        fallback = await asyncio.gather(*(_ipwho_lookup(ip) for ip in unresolved))
        online.update({ip: info for ip, info in zip(unresolved, fallback) if info})

    for ip in missing:
        info = online.get(ip)
        if info is None:
            # Local databases remain useful when both online services are unavailable.
            local = _from_city_db(ip) or _from_country_db(ip)
            info = local or _empty_info(ip)
        else:
            info = dict(info)
        info["ip"] = ip
        localized_country = country_name_zh(info.get("country"))
        if localized_country and localized_country != info.get("country"):
            info["country_name"] = localized_country
        else:
            info["country_name"] = info.get("country_name") or localized_country
        result[ip] = info
        _cache[ip] = (now, info)
    return [result[ip] for ip in ordered]


async def lookup_geo(ip: str) -> dict[str, Any]:
    items = await lookup_geo_batch([ip])
    return items[0] if items else _empty_info(ip)


def format_city(info: dict[str, Any]) -> str:
    province = str(info.get("province") or "").strip()
    city = str(info.get("city") or "").strip()
    if not province:
        return city
    if not city:
        return province
    if province in city or city in province:
        return city
    return f"{province}{city}"
