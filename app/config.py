from __future__ import annotations

import os
from dataclasses import dataclass

APP_NAME = os.getenv("APP_NAME", "RouteCheck")
APP_VERSION = os.getenv("APP_VERSION", "V2026.9.16")
PROJECT_URL = "https://github.com/Seven1echo/RouteCheck"


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


@dataclass(frozen=True)
class Settings:
    mihomo_url: str = os.getenv("MIHOMO_URL", "http://192.168.1.1:9090").rstrip("/")
    mihomo_secret: str = os.getenv("MIHOMO_SECRET", "")
    poll_interval: int = _int("POLL_INTERVAL_SECONDS", 5)
    capture_mode: str = os.getenv("CAPTURE_MODE", "leak_rule").lower()
    leak_rule_payloads: tuple[str, ...] = tuple(
        x.strip().lower() for x in os.getenv("LEAK_RULE_PAYLOADS", "漏网之鱼,MATCH").split(",") if x.strip()
    )
    analyze_interval: int = _int("ANALYZE_INTERVAL_SECONDS", 60)
    analyze_concurrency: int = max(1, min(16, _int("ANALYZE_CONCURRENCY", 4)))
    probe_timeout: int = _int("DIRECT_PROBE_TIMEOUT_SECONDS", 5)
    auto_direct_score: float = _float("AUTO_DIRECT_SCORE", 0.85)
    geoip_db_path: str = os.getenv("GEOIP_DB_PATH", "/geoip/GeoLite2-Country.mmdb")
    geoip_city_db_path: str = os.getenv("GEOIP_CITY_DB_PATH", "/geoip/GeoLite2-City.mmdb")
    geoip_country_url: str = os.getenv(
        "GEOIP_COUNTRY_URL",
        "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/country.mmdb",
    )
    geoip_city_url: str = os.getenv(
        "GEOIP_CITY_URL",
        "https://github.com/P3TERX/GeoLite.mmdb/releases/latest/download/GeoLite2-City.mmdb",
    )
    auto_download_geoip: bool = _bool("AUTO_DOWNLOAD_GEOIP", True)
    online_geo_lookup: bool = _bool("ONLINE_GEO_LOOKUP", True)
    geo_lookup_url: str = os.getenv(
        "GEO_LOOKUP_URL",
        "http://ip-api.com/json/{ip}?lang=zh-CN&fields=status,message,country,countryCode,regionName,city",
    )
    geo_lookup_timeout: int = _int("GEO_LOOKUP_TIMEOUT_SECONDS", 4)
    database_path: str = os.getenv("DATABASE_PATH", "/data/routecheck.db")


settings = Settings()
