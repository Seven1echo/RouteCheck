from __future__ import annotations

import asyncio
import ipaddress
from typing import Any

import httpx

from .config import APP_VERSION, settings
from .countries import country_name_zh
from .db import ANALYSIS_SCOPES, list_for_analysis, update_analysis
from .geo import flag_for, format_city, lookup_geo_batch
from .dns import resolve_domain, resolve_system
from . import runtime_config


async def direct_probe(domain: str) -> dict[str, Any]:
    # trust_env=False is intentional: the analysis must not silently use a proxy.
    last = "unknown"
    for scheme in ("https", "http"):
        try:
            async with httpx.AsyncClient(timeout=settings.probe_timeout, follow_redirects=True, trust_env=False) as client:
                response = await client.get(
                    f"{scheme}://{domain}/",
                    headers={"User-Agent": f"routecheck/{APP_VERSION}"},
                )
            return {"ok": True, "scheme": scheme, "status_code": response.status_code}
        except (httpx.HTTPError, OSError) as exc:
            last = type(exc).__name__
    return {"ok": False, "error": last}


async def resolve_fallback(domain: str) -> list[str]:
    try:
        return await resolve_system(domain)
    except OSError:
        return []


def _is_private(addresses: list[str]) -> bool:
    if not addresses:
        return False
    try:
        return all(ipaddress.ip_address(ip).is_private for ip in addresses)
    except ValueError:
        return False


def _matches_cn(geo: list[dict[str, Any]], mode: str) -> bool:
    if not geo:
        return False
    codes = [str(item.get("country") or "").upper() for item in geo]
    if mode == "any":
        return any(code == "CN" for code in codes)
    if mode == "first":
        return codes[0] == "CN"
    # The default is deliberately conservative: unknown or non-CN IPs fail too.
    return len(codes) == len(geo) and all(code == "CN" for code in codes)


async def analyze_domain(domain: str) -> dict[str, Any]:
    try:
        addresses, resolver = await resolve_domain(domain)
    except Exception:
        addresses, resolver = [], "unresolved"

    probe = await direct_probe(domain)
    geo: list[dict[str, Any]] = await lookup_geo_batch(addresses) if addresses else []

    countries = sorted({str(item.get("country") or "") for item in geo if item.get("country")})
    cn_entries = [item for item in geo if item.get("country") == "CN"]
    primary = next((item for item in cn_entries if item.get("city")), None) or next((item for item in geo if item.get("city")), None)
    primary = primary or (geo[0] if geo else {})
    country = countries[0] if len(countries) == 1 else ""
    country_name = str(primary.get("country_name") or "") or country_name_zh(country)
    city = format_city(primary) if primary else ""
    match_mode = runtime_config.cn_match_mode()
    is_cn = _matches_cn(geo, match_mode)
    direct_ok = bool(probe.get("ok"))
    cn_direct = is_cn and direct_ok
    private = _is_private(addresses)
    cn_tld = domain.endswith((".cn", ".中国", ".公司", ".网络"))

    reasons: list[str] = []
    reasons.append(f"DNS：{resolver}")
    if direct_ok:
        reasons.append(f"直连探测成功（{probe.get('scheme')} {probe.get('status_code')}）")
    if is_cn:
        flag = flag_for("CN")
        mode_text = {"all": "全部", "any": "任一", "first": "首个"}.get(match_mode, "全部")
        reasons.append(f"按{mode_text} IP 判定为中国（countryCode=CN） {flag}".strip())
        if city:
            reasons.append(f"归属地：{city}")
    else:
        if not geo:
            reasons.append("没有解析到 IP")
        elif any(item.get("country") and item.get("country") != "CN" for item in geo):
            reasons.append("存在 countryCode 非 CN 的解析地址")
        else:
            reasons.append("IP 归属地查询失败，无法判定为中国")
    if private:
        reasons.append("存在非中国解析地址")

    evidence = {
        "addresses": addresses,
        "resolver": resolver,
        "geo": geo,
        "countries": countries,
        "country": country,
        "country_name": country_name,
        "city": city,
        "is_cn": is_cn,
        "cn_direct": cn_direct,
        "direct_probe": probe,
        "cn_tld": cn_tld,
        "private": private,
        "cn_match_mode": match_mode,
    }
    status = "direct_candidate" if is_cn else "review"
    return {
        "domain": domain,
        "reason": "；".join(reasons) or "证据不足，建议人工审核",
        "evidence": evidence,
        "status": status,
        "country": country,
        "country_name": country_name,
        "city": city,
        "is_cn": is_cn,
        "direct_ok": direct_ok,
        "cn_direct": cn_direct,
    }


KEEP_STATUS = {"approved", "rejected"}


async def analyze_batch(scope: str = "pending", limit: int = 50) -> dict[str, Any]:
    """重新分析一批域名。已批准/已拒绝的记录只刷新证据，状态保持不变。"""
    scope = scope if scope in ANALYSIS_SCOPES else "pending"
    rows = await list_for_analysis(scope, limit)
    semaphore = asyncio.Semaphore(max(1, settings.analyze_concurrency))

    async def worker(item: dict[str, Any]) -> bool:
        async with semaphore:
            result = await analyze_domain(item["domain"])
            status = item["status"] if item["status"] in KEEP_STATUS else result["status"]
            await update_analysis(
                item["domain"],
                result["reason"],
                result["evidence"],
                status,
                country=result["country"],
                country_name=result["country_name"],
                city=result["city"],
                is_cn=result["is_cn"],
                direct_ok=result["direct_ok"],
                cn_direct=result["cn_direct"],
            )
            return True

    if not rows:
        return {"analyzed": 0, "matched": 0, "failed": 0, "scope": scope}
    outcomes = await asyncio.gather(*(worker(item) for item in rows), return_exceptions=True)
    return {
        "analyzed": sum(1 for item in outcomes if item is True),
        "matched": len(rows),
        "failed": sum(1 for item in outcomes if item is not True),
        "scope": scope,
    }


async def analyze_pending(limit: int = 50) -> int:
    return int((await analyze_batch("pending", limit))["analyzed"])
