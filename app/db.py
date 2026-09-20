from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .config import settings


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _prepare_database_path() -> Path:
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 老版本（Mihomo Direct Tester / Mihomo Leak Hunter）的库自动沿用，避免丢数据
    if not os.getenv("DATABASE_PATH") and not path.exists():
        for legacy_name in ("mihomo-direct-tester.db", "leak-hunter.db"):
            legacy = path.with_name(legacy_name)
            if legacy.exists():
                try:
                    shutil.copy2(legacy, path)
                except OSError:
                    pass
                break
    return path


DOMAIN_COLUMNS: dict[str, str] = {
    "country": "TEXT NOT NULL DEFAULT ''",
    "country_name": "TEXT NOT NULL DEFAULT ''",
    "city": "TEXT NOT NULL DEFAULT ''",
    "is_cn": "INTEGER NOT NULL DEFAULT 0",
    "direct_ok": "INTEGER NOT NULL DEFAULT 0",
    "cn_direct": "INTEGER NOT NULL DEFAULT 0",
    "rule_type": "TEXT NOT NULL DEFAULT ''",
}


async def init_db() -> None:
    _prepare_database_path()
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS domains (
              domain TEXT PRIMARY KEY,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              hit_count INTEGER NOT NULL DEFAULT 1,
              status TEXT NOT NULL DEFAULT 'pending',
              score REAL NOT NULL DEFAULT 0,
              reason TEXT NOT NULL DEFAULT '',
              evidence_json TEXT NOT NULL DEFAULT '{}',
              last_rule TEXT NOT NULL DEFAULT '',
              last_policy TEXT NOT NULL DEFAULT '',
              last_source TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_domains_status ON domains(status)")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        cur = await db.execute("PRAGMA table_info(domains)")
        existing = {row[1] for row in await cur.fetchall()}
        for name, definition in DOMAIN_COLUMNS.items():
            if name not in existing:
                await db.execute(f"ALTER TABLE domains ADD COLUMN {name} {definition}")
        await db.commit()


async def record_connection(domain: str, rule: str, policy: str, source: str) -> None:
    ts = now_iso()
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """
            INSERT INTO domains(domain, first_seen, last_seen, hit_count, last_rule, last_policy, last_source, updated_at)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
              last_seen=excluded.last_seen,
              hit_count=hit_count+1,
              last_rule=excluded.last_rule,
              last_policy=excluded.last_policy,
              last_source=excluded.last_source,
              updated_at=excluded.updated_at
            """,
            (domain, ts, ts, rule, policy, source, ts),
        )
        await db.commit()


async def list_domains(status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        if status:
            cur = await db.execute("SELECT * FROM domains WHERE status=? ORDER BY hit_count DESC, last_seen DESC LIMIT ?", (status, limit))
        else:
            cur = await db.execute("SELECT * FROM domains ORDER BY hit_count DESC, last_seen DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
        item["is_cn"] = bool(item.get("is_cn"))
        item["direct_ok"] = bool(item.get("direct_ok"))
        item["cn_direct"] = bool(item.get("cn_direct"))
        result.append(item)
    return result


ANALYSIS_SCOPES: dict[str, str] = {
    "pending": "status='pending'",
    "unresolved": "status IN ('review','direct_candidate')",
    "missing_geo": "(country='' OR country_name='' OR city='')",
    "all": "1=1",
}


async def list_for_analysis(scope: str, limit: int = 50) -> list[dict[str, Any]]:
    where = ANALYSIS_SCOPES.get(scope, ANALYSIS_SCOPES["pending"])
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT domain, status FROM domains WHERE {where} ORDER BY hit_count DESC, last_seen DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
    return [{"domain": str(row["domain"]), "status": str(row["status"])} for row in rows]


async def update_analysis(
    domain: str,
    score: float,
    reason: str,
    evidence: dict[str, Any],
    status: str,
    country: str = "",
    country_name: str = "",
    city: str = "",
    is_cn: bool = False,
    direct_ok: bool = False,
    cn_direct: bool = False,
) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """
            UPDATE domains SET score=?, reason=?, evidence_json=?, status=?, country=?, country_name=?, city=?,
                   is_cn=?, direct_ok=?, cn_direct=?, updated_at=?
            WHERE domain=?
            """,
            (
                score,
                reason,
                json.dumps(evidence, ensure_ascii=False),
                status,
                country,
                country_name,
                city,
                int(is_cn),
                int(direct_ok),
                int(cn_direct),
                now_iso(),
                domain,
            ),
        )
        await db.commit()


async def set_status(domain: str, status: str) -> bool:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("UPDATE domains SET status=?, updated_at=? WHERE domain=?", (status, now_iso(), domain))
        await db.commit()
        return cur.rowcount > 0


async def set_rule_type(domain: str, rule_type: str) -> bool:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("UPDATE domains SET rule_type=?, updated_at=? WHERE domain=?", (rule_type, now_iso(), domain))
        await db.commit()
        return cur.rowcount > 0


async def stats() -> dict[str, int]:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT status, COUNT(*) FROM domains GROUP BY status")
        rows = await cur.fetchall()
    result = {row[0]: row[1] for row in rows}
    result["total"] = sum(result.values())
    return result


IMPORT_COLUMNS = (
    "domain",
    "first_seen",
    "last_seen",
    "hit_count",
    "status",
    "score",
    "reason",
    "evidence_json",
    "last_rule",
    "last_policy",
    "last_source",
    "updated_at",
    "country",
    "country_name",
    "city",
    "is_cn",
    "direct_ok",
    "cn_direct",
    "rule_type",
)

KNOWN_STATUSES = {"pending", "review", "direct_candidate", "approved", "rejected"}


def _import_row(item: dict[str, Any]) -> dict[str, Any]:
    ts = now_iso()
    evidence = item.get("evidence", item.get("evidence_json", {}))
    if isinstance(evidence, str):
        evidence_json = evidence
    else:
        evidence_json = json.dumps(evidence or {}, ensure_ascii=False)
    status = str(item.get("status") or "pending")
    if status not in KNOWN_STATUSES:
        status = "pending"
    try:
        hit_count = max(1, int(item.get("hit_count") or 1))
    except (TypeError, ValueError):
        hit_count = 1
    try:
        score = float(item.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    return {
        "domain": str(item.get("domain") or "").strip().lower(),
        "first_seen": str(item.get("first_seen") or ts),
        "last_seen": str(item.get("last_seen") or ts),
        "hit_count": hit_count,
        "status": status,
        "score": score,
        "reason": str(item.get("reason") or ""),
        "evidence_json": evidence_json,
        "last_rule": str(item.get("last_rule") or ""),
        "last_policy": str(item.get("last_policy") or ""),
        "last_source": str(item.get("last_source") or ""),
        "updated_at": str(item.get("updated_at") or ts),
        "country": str(item.get("country") or "").upper(),
        "country_name": str(item.get("country_name") or ""),
        "city": str(item.get("city") or ""),
        "is_cn": int(bool(item.get("is_cn"))),
        "direct_ok": int(bool(item.get("direct_ok"))),
        "cn_direct": int(bool(item.get("cn_direct"))),
        "rule_type": str(item.get("rule_type") or "").upper(),
    }


async def export_all() -> list[dict[str, Any]]:
    return await list_domains(limit=1_000_000)


async def import_domains(items: list[Any]) -> dict[str, int]:
    inserted = updated = skipped = 0
    async with aiosqlite.connect(settings.database_path) as db:
        for item in items:
            if not isinstance(item, dict):
                skipped += 1
                continue
            row = _import_row(item)
            if not row["domain"]:
                skipped += 1
                continue
            cur = await db.execute("SELECT 1 FROM domains WHERE domain=?", (row["domain"],))
            exists = await cur.fetchone() is not None
            values = [row[name] for name in IMPORT_COLUMNS]
            if exists:
                assignments = ", ".join(f"{name}=?" for name in IMPORT_COLUMNS[1:])
                await db.execute(f"UPDATE domains SET {assignments} WHERE domain=?", values[1:] + [row["domain"]])
                updated += 1
            else:
                placeholders = ", ".join("?" for _ in IMPORT_COLUMNS)
                await db.execute(f"INSERT INTO domains({', '.join(IMPORT_COLUMNS)}) VALUES ({placeholders})", values)
                inserted += 1
        await db.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped, "total": inserted + updated}


async def clear_domains() -> int:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("DELETE FROM domains")
        await db.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


async def settings_get_all() -> dict[str, str]:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT key, value FROM settings")
        rows = await cur.fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


async def settings_put_all(values: dict[str, str]) -> None:
    if not values:
        return
    ts = now_iso()
    async with aiosqlite.connect(settings.database_path) as db:
        await db.executemany(
            """
            INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            [(key, str(value), ts) for key, value in values.items()],
        )
        await db.commit()


async def settings_delete_all() -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute("DELETE FROM settings")
        await db.commit()
