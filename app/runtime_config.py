from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from .config import settings
from .db import settings_delete_all, settings_get_all, settings_put_all


CAPTURE_MODES = ("leak_rule", "all_proxy")
OVERRIDE_KEYS = (
    "mihomo_host",
    "mihomo_port",
    "mihomo_secret",
    "poll_interval",
    "capture_mode",
    "online_geo_lookup",
    "geoip_country_url",
    "geoip_city_url",
    "auto_download_geoip",
    "geoip_db_path",
    "geoip_city_db_path",
)

_lock = asyncio.Lock()
_overrides: dict[str, str] = {}


def _env_host_port() -> tuple[str, int]:
    raw = settings.mihomo_url or "http://192.168.1.1:9090"
    parsed = urlsplit(raw if "://" in raw else f"http://{raw}")
    return parsed.hostname or "192.168.1.1", parsed.port or 9090


def _to_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text == "":
        return default
    return text in {"1", "true", "yes", "on", "y"}


def _clean_host(raw: Any) -> tuple[str, int | None]:
    """Accept 192.168.1.1, http://192.168.1.1:9090 or 192.168.1.1:9090."""
    value = str(raw or "").strip()
    if not value:
        raise ValueError("控制器地址不能为空")
    if "://" in value:
        parsed = urlsplit(value)
        value = parsed.netloc or parsed.path
    value = value.split("/")[0].strip()
    port: int | None = None
    if value.startswith("[") and "]" in value:
        host, _, rest = value[1:].partition("]")
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
        return host, port
    if value.count(":") == 1:
        host, _, tail = value.partition(":")
        if tail.isdigit():
            return host, int(tail)
    if not value or any(ch.isspace() for ch in value):
        raise ValueError("控制器地址格式不正确")
    return value, port


def _effective() -> dict[str, Any]:
    env_host, env_port = _env_host_port()
    host = str(_overrides.get("mihomo_host") or env_host)
    port = _to_int(_overrides.get("mihomo_port"), env_port)
    if not 1 <= port <= 65535:
        port = env_port
    secret = _overrides["mihomo_secret"] if "mihomo_secret" in _overrides else settings.mihomo_secret
    mode = str(_overrides.get("capture_mode") or settings.capture_mode).lower()
    if mode not in CAPTURE_MODES:
        mode = "leak_rule"
    return {
        "mihomo_host": host,
        "mihomo_port": port,
        "mihomo_secret": secret,
        "poll_interval": max(2, _to_int(_overrides.get("poll_interval"), settings.poll_interval)),
        "capture_mode": mode,
        "online_geo_lookup": _to_bool(_overrides.get("online_geo_lookup"), settings.online_geo_lookup),
        "geoip_country_url": str(_overrides.get("geoip_country_url") or settings.geoip_country_url),
        "geoip_city_url": str(_overrides.get("geoip_city_url") or settings.geoip_city_url),
        "auto_download_geoip": _to_bool(_overrides.get("auto_download_geoip"), settings.auto_download_geoip),
        "geoip_db_path": str(_overrides.get("geoip_db_path") or settings.geoip_db_path),
        "geoip_city_db_path": str(_overrides.get("geoip_city_db_path") or settings.geoip_city_db_path),
    }


def defaults() -> dict[str, Any]:
    env_host, env_port = _env_host_port()
    return {
        "mihomo_host": env_host,
        "mihomo_port": env_port,
        "mihomo_secret": settings.mihomo_secret,
        "poll_interval": settings.poll_interval,
        "capture_mode": settings.capture_mode if settings.capture_mode in CAPTURE_MODES else "leak_rule",
        "online_geo_lookup": settings.online_geo_lookup,
        "geoip_country_url": settings.geoip_country_url,
        "geoip_city_url": settings.geoip_city_url,
        "auto_download_geoip": settings.auto_download_geoip,
        "geoip_db_path": settings.geoip_db_path,
        "geoip_city_db_path": settings.geoip_city_db_path,
    }


async def load() -> None:
    stored = await settings_get_all()
    async with _lock:
        _overrides.clear()
        _overrides.update({key: value for key, value in stored.items() if key in OVERRIDE_KEYS})


def snapshot() -> dict[str, Any]:
    data = _effective()
    data["defaults"] = defaults()
    data["overridden"] = sorted(key for key in _overrides if key in OVERRIDE_KEYS)
    data["capture_modes"] = list(CAPTURE_MODES)
    return data


def _clean_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        raise ValueError("下载地址必须以 http:// 或 https:// 开头")
    if len(text) > 500:
        raise ValueError("下载地址过长")
    return text


async def update(values: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, str] = {}
    if "mihomo_host" in values:
        host, port = _clean_host(values.get("mihomo_host"))
        clean["mihomo_host"] = host
        if port:
            clean["mihomo_port"] = str(port)
    if "mihomo_port" in values:
        port = _to_int(values.get("mihomo_port"), 0)
        if not 1 <= port <= 65535:
            raise ValueError("端口号必须在 1-65535 之间")
        clean["mihomo_port"] = str(port)
    if "mihomo_secret" in values:
        clean["mihomo_secret"] = str(values.get("mihomo_secret") or "").strip()
    if "poll_interval" in values:
        clean["poll_interval"] = str(max(2, min(600, _to_int(values.get("poll_interval"), settings.poll_interval))))
    if "capture_mode" in values:
        mode = str(values.get("capture_mode") or "").strip().lower()
        if mode not in CAPTURE_MODES:
            raise ValueError("采集模式必须是 leak_rule 或 all_proxy")
        clean["capture_mode"] = mode
    if "online_geo_lookup" in values:
        clean["online_geo_lookup"] = "1" if _to_bool(values.get("online_geo_lookup"), False) else "0"
    for key in ("geoip_country_url", "geoip_city_url"):
        if key in values:
            clean[key] = _clean_url(values.get(key))
    if "auto_download_geoip" in values:
        clean["auto_download_geoip"] = "1" if _to_bool(values.get("auto_download_geoip"), False) else "0"
    for key in ("geoip_db_path", "geoip_city_db_path"):
        if key in values:
            path = str(values.get(key) or "").strip()
            if path and not path.startswith("/"):
                raise ValueError(f"{key} 必须是容器内的绝对路径")
            clean[key] = path
    async with _lock:
        _overrides.update(clean)
    await settings_put_all(clean)
    return snapshot()


async def reset() -> dict[str, Any]:
    async with _lock:
        _overrides.clear()
    await settings_delete_all()
    return snapshot()


def mihomo_base_url() -> str:
    data = _effective()
    return f"http://{data['mihomo_host']}:{data['mihomo_port']}"


def mihomo_secret() -> str:
    return str(_effective()["mihomo_secret"])


def poll_interval() -> int:
    return int(_effective()["poll_interval"])


def capture_mode() -> str:
    return str(_effective()["capture_mode"])


def online_geo_lookup() -> bool:
    return bool(_effective()["online_geo_lookup"])


def geoip_url(kind: str) -> str:
    return str(_effective()["geoip_city_url" if kind == "city" else "geoip_country_url"])


def geoip_path(kind: str) -> str:
    return str(_effective()["geoip_city_db_path" if kind == "city" else "geoip_db_path"])


def auto_download_geoip() -> bool:
    return bool(_effective()["auto_download_geoip"])
