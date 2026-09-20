from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, Response

from . import runtime_config
from .analyzer import analyze_batch, analyze_pending
from .config import APP_NAME, APP_VERSION, PROJECT_URL, settings
from .countries import country_name_zh
from .db import (
    ANALYSIS_SCOPES,
    clear_domains,
    export_all,
    import_domains,
    init_db,
    list_domains,
    record_connection,
    set_rule_type,
    set_status,
    stats,
)
from .geo import flag_for
from .geoip import auto_download_missing
from .db import now_iso
from .geoip import download as geoip_download
from .geoip import status as geoip_status
from .mihomo import controller_status, extract_host, fetch_connections, is_leak
from .rules import (
    build_rule_items,
    effective_rule_type,
    exported_payloads,
    normalize_rule_type,
    registrable_domain,
    render_classic_rules,
    render_rule_items,
    rule_payload,
    suffix_sites,
)


async def collector_loop() -> None:
    seen_connection_ids: set[str] = set()
    while True:
        try:
            connections = await fetch_connections()
            current_ids = set()
            for connection in connections:
                cid = str(connection.get("id", ""))
                if cid:
                    current_ids.add(cid)
                if cid and cid in seen_connection_ids:
                    continue
                if not is_leak(connection):
                    continue
                domain = extract_host(connection)
                if not domain:
                    continue
                await record_connection(
                    domain,
                    str(connection.get("rule", "")),
                    str(connection.get("rulePayload", "")),
                    str((connection.get("metadata") or {}).get("sourceIP", "")),
                )
            seen_connection_ids = current_ids
        except Exception:
            # The UI remains available even while the router is offline.
            pass
        await asyncio.sleep(max(2, runtime_config.poll_interval()))


async def analyzer_loop() -> None:
    while True:
        with contextlib.suppress(Exception):
            await analyze_pending()
        await asyncio.sleep(max(15, settings.analyze_interval))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await runtime_config.load()
    tasks = [
        asyncio.create_task(collector_loop()),
        asyncio.create_task(analyzer_loop()),
        asyncio.create_task(auto_download_missing()),
    ]
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    config = runtime_config.snapshot()
    return {
        "status": "ok",
        "app": APP_NAME,
        "version": APP_VERSION,
        "project_url": PROJECT_URL,
        "capture_mode": config["capture_mode"],
        "mihomo_url": runtime_config.mihomo_base_url(),
    }


@app.get("/api/data/export")
async def export_data():
    rows = await export_all()
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "project_url": PROJECT_URL,
        "exported_at": now_iso(),
        "count": len(rows),
        "domains": rows,
    }


@app.post("/api/data/import")
async def import_data(payload: dict[str, Any] = Body(default_factory=dict)):
    body = payload or {}
    items = body.get("domains") if isinstance(body.get("domains"), list) else (body if isinstance(body, list) else None)
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="导入内容必须是包含 domains 数组的 JSON")
    return await import_domains(items)


@app.post("/api/data/clear")
async def clear_data(payload: dict[str, Any] = Body(default_factory=dict)):
    if str((payload or {}).get("confirm") or "").strip().lower() not in {"yes", "true", "1", "清空"}:
        raise HTTPException(status_code=400, detail="清空数据需要 body 里带 confirm=yes")
    return {"cleared": await clear_domains()}


@app.get("/api/stats")
async def get_stats() -> dict[str, int]:
    return await stats()


@app.get("/api/candidates")
async def candidates(status: str | None = Query(default=None), limit: int = Query(default=500, ge=1, le=5000)):
    rows = await list_domains(status=status, limit=limit)
    items = build_rule_items(await list_domains(limit=5000))
    exported = exported_payloads(items)
    suffixes = suffix_sites(items)
    for row in rows:
        domain = str(row.get("domain") or "")
        rule_type = effective_rule_type(domain, row.get("rule_type"))
        payload = rule_payload(domain, rule_type)
        row["country_name"] = row.get("country_name") or country_name_zh(row.get("country"))
        row["flag"] = flag_for(row.get("country"))
        row["rule_type_effective"] = rule_type
        row["rule_payload"] = payload
        row["rule_auto"] = not row.get("rule_type")
        row["rule_exported"] = payload in exported
        site = registrable_domain(domain)
        row["rule_covered_by"] = f"+.{site}" if site in suffixes and payload not in exported else ""
    return rows


@app.get("/api/geoip")
async def get_geoip_status() -> dict[str, Any]:
    return geoip_status()


@app.post("/api/geoip/download")
async def download_geoip(payload: dict[str, Any] = Body(default_factory=dict)):
    body = payload or {}
    try:
        return await geoip_download(str(body.get("kind") or "country"), body.get("url"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return runtime_config.snapshot()


@app.post("/api/settings")
async def save_settings(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    try:
        return await runtime_config.update(payload or {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings/reset")
async def reset_settings() -> dict[str, Any]:
    return await runtime_config.reset()


@app.post("/api/settings/test")
async def test_settings() -> dict[str, Any]:
    return await controller_status()


@app.post("/api/analyze")
async def analyze(payload: dict[str, Any] = Body(default_factory=dict)):
    body = payload or {}
    scope = str(body.get("scope") or "pending")
    if scope not in ANALYSIS_SCOPES:
        raise HTTPException(status_code=400, detail="scope 只能是 " + "、".join(ANALYSIS_SCOPES))
    try:
        limit = int(body.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    return await analyze_batch(scope, max(1, min(500, limit)))


@app.post("/api/domains/{domain}/approve")
async def approve(domain: str):
    if not await set_status(domain, "approved"):
        raise HTTPException(status_code=404, detail="domain not found")
    return {"domain": domain, "status": "approved"}


@app.post("/api/domains/{domain}/reject")
async def reject(domain: str):
    if not await set_status(domain, "rejected"):
        raise HTTPException(status_code=404, detail="domain not found")
    return {"domain": domain, "status": "rejected"}


@app.post("/api/domains/{domain}/rule-type")
async def change_rule_type(domain: str, payload: dict[str, Any] = Body(default_factory=dict)):
    try:
        value = normalize_rule_type((payload or {}).get("rule_type"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not await set_rule_type(domain, value):
        raise HTTPException(status_code=404, detail="domain not found")
    return {
        "domain": domain,
        "rule_type": value or "auto",
        "rule_type_effective": effective_rule_type(domain, value),
        "rule_payload": rule_payload(domain, effective_rule_type(domain, value)),
    }


@app.get("/api/rules/direct.txt", response_class=PlainTextResponse)
async def direct_rules_text():
    items = build_rule_items(await list_domains(limit=5000))
    return PlainTextResponse(render_rule_items(items), media_type="text/plain; charset=utf-8")


@app.get("/api/rules/direct.yaml")
async def direct_rules_yaml():
    items = build_rule_items(await list_domains(limit=5000))
    body = render_classic_rules(items, APP_NAME, APP_VERSION)
    return Response(content=body, media_type="text/yaml; charset=utf-8")


@app.get("/api/rules/direct.json")
async def direct_rules_json():
    rows = await list_domains(status="approved", limit=5000)
    items = build_rule_items(await list_domains(limit=5000))
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "count": len(rows),
        "rules": items,
        "domains": [
            {
                "domain": row["domain"],
                "country": row.get("country", ""),
                "flag": flag_for(row.get("country")),
                "city": row.get("city", ""),
                "score": round(float(row.get("score") or 0), 3),
                "rule_type": effective_rule_type(row["domain"], row.get("rule_type")),
                "rule_payload": rule_payload(row["domain"], effective_rule_type(row["domain"], row.get("rule_type"))),
                "rule_override": bool(row.get("rule_type")),
            }
            for row in rows
        ],
    }
