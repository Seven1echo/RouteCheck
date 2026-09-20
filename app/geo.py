from __future__ import annotations

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


def _parse_online(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("status") == "fail":
        return None
    code = payload.get("countryCode") or payload.get("country_code") or payload.get("country_code2") or ""
    code = str(code).strip().upper()
    if len(code) != 2 and payload.get("status") == "fail":
        return None
    return {
        "country": code if len(code) == 2 else "",
        "country_name": str(payload.get("country") or payload.get("country_name") or ""),
        "province": str(payload.get("regionName") or payload.get("region") or payload.get("province") or ""),
        "city": str(payload.get("city") or payload.get("city_name") or ""),
        "source": "online",
    }


async def _online_lookup(ip: str) -> dict[str, Any] | None:
    if not settings.geo_lookup_url:
        return None
    try:
        url = settings.geo_lookup_url.format(ip=ip)
    except (KeyError, IndexError):
        return None
    try:
        async with httpx.AsyncClient(timeout=settings.geo_lookup_timeout, trust_env=False) as client:
            response = await client.get(url, headers={"User-Agent": "routecheck/1.0"})
            response.raise_for_status()
        return _parse_online(response.json())
    except Exception:
        return None


async def lookup_geo(ip: str) -> dict[str, Any]:
    now = time.time()
    cached = _cache.get(ip)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    info = _from_city_db(ip) or _from_country_db(ip) or {"country": "", "country_name": "", "province": "", "city": "", "source": "none"}
    if not info.get("city") and runtime_config.online_geo_lookup():
        online = await _online_lookup(ip)
        if online:
            info = {
                "country": info.get("country") or online.get("country", ""),
                "country_name": info.get("country_name") or online.get("country_name", ""),
                "province": info.get("province") or online.get("province", ""),
                "city": online.get("city", ""),
                "source": f"{info.get('source')}+online" if info.get("country") else "online",
            }
    info = dict(info)
    info["ip"] = ip
    info["country_name"] = info.get("country_name") or country_name_zh(info.get("country"))
    _cache[ip] = (now, info)
    return info


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
