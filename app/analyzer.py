from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any

import httpx

from .config import APP_VERSION, settings
from .countries import country_name_zh
from .db import ANALYSIS_SCOPES, list_for_analysis, update_analysis
from .geo import flag_for, format_city, lookup_geo
from .mihomo import query_dns


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
        infos = await asyncio.to_thread(socket.getaddrinfo, domain, 443, type=socket.SOCK_STREAM)
        return sorted({item[4][0] for item in infos})
    except OSError:
        return []


def _is_private(addresses: list[str]) -> bool:
    if not addresses:
        return False
    try:
        return all(ipaddress.ip_address(ip).is_private for ip in addresses)
    except ValueError:
        return False


async def analyze_domain(domain: str) -> dict[str, Any]:
    try:
        addresses = await query_dns(domain)
    except Exception:
        addresses = await resolve_fallback(domain)

    probe = await direct_probe(domain)
    geo: list[dict[str, Any]] = list(await asyncio.gather(*(lookup_geo(ip) for ip in addresses))) if addresses else []

    countries = sorted({str(item.get("country") or "") for item in geo if item.get("country")})
    cn_entries = [item for item in geo if item.get("country") == "CN"]
    primary = next((item for item in cn_entries if item.get("city")), None) or next((item for item in geo if item.get("city")), None)
    primary = primary or (geo[0] if geo else {})
    country = countries[0] if len(countries) == 1 else ""
    country_name = str(primary.get("country_name") or "") or country_name_zh(country)
    city = format_city(primary) if primary else ""
    is_cn = bool(countries) and countries == ["CN"]
    direct_ok = bool(probe.get("ok"))
    cn_direct = is_cn and direct_ok
    private = _is_private(addresses)
    cn_tld = domain.endswith((".cn", ".中国", ".公司", ".网络"))

    score = 0.0
    reasons: list[str] = []
    if private:
        score += 1.0
        reasons.append("解析结果属于内网/保留地址")
    if direct_ok:
        score += 0.25
        reasons.append(f"直连探测成功（{probe.get('scheme')} {probe.get('status_code')}）")
    if cn_tld:
        score += 0.35
        reasons.append("域名使用中国相关后缀")
    if is_cn:
        score += 0.65
        flag = flag_for("CN")
        reasons.append(f"解析地址均为国内 IP {flag}".strip())
        if city:
            reasons.append(f"归属地：{city}")
    if any(item.get("country") and item.get("country") != "CN" for item in geo):
        score -= 0.6
        reasons.append("存在非中国解析地址")
    score = max(0.0, min(1.0, score))

    evidence = {
        "addresses": addresses,
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
        "score": round(score, 3),
    }
    status = "direct_candidate" if score >= settings.auto_direct_score else "review"
    if private:
        status = "direct_candidate"
    return {
        "domain": domain,
        "score": score,
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
                result["score"],
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
