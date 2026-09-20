from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import httpx

from . import runtime_config


USER_AGENT = "routecheck/1.0"
MAX_BYTES = 200 * 1024 * 1024
FALLBACK_DIR = Path("/data/geoip")

KINDS = ("country", "city")

_lock = asyncio.Lock()
_task: asyncio.Task[None] | None = None
_state: dict[str, Any] = {
    "running": False,
    "kind": "",
    "url": "",
    "ok": None,
    "message": "尚未下载过",
    "bytes": 0,
    "path": "",
    "started_at": 0.0,
    "finished_at": 0.0,
}


_writable_cache: dict[str, tuple[float, bool]] = {}


def _dir_writable(directory: Path, ttl: float = 30.0) -> bool:
    key = str(directory)
    cached = _writable_cache.get(key)
    now = time.time()
    if cached and now - cached[0] < ttl:
        return cached[1]
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_bytes(b"")
        probe.unlink()
        result = True
    except OSError:
        result = False
    _writable_cache[key] = (now, result)
    return result


def target_path(kind: str) -> Path:
    """优先写进配置的目录（通常是 /geoip），不可写时退回 /data/geoip。"""
    configured = Path(runtime_config.geoip_path(kind))
    if _dir_writable(configured.parent):
        return configured
    if _dir_writable(FALLBACK_DIR):
        return FALLBACK_DIR / configured.name
    return configured


def _database_type(path: Path) -> str:
    import maxminddb

    with maxminddb.open_database(str(path)) as reader:
        return str(reader.metadata().database_type)


def status_for(kind: str) -> dict[str, Any]:
    path = Path(runtime_config.geoip_path(kind))
    info: dict[str, Any] = {
        "kind": kind,
        "path": str(path),
        "url": runtime_config.geoip_url(kind),
        "exists": path.exists(),
        "size": 0,
        "mtime": "",
        "database_type": "",
        "writable_dir": _dir_writable(path.parent),
    }
    if path.exists():
        stat = path.stat()
        info["size"] = stat.st_size
        info["mtime"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
        try:
            info["database_type"] = _database_type(path)
        except Exception as exc:
            info["database_type"] = f"无法读取：{type(exc).__name__}"
    return info


def status() -> dict[str, Any]:
    return {
        "files": {kind: status_for(kind) for kind in KINDS},
        "download": dict(_state),
        "auto_download": runtime_config.auto_download_geoip(),
    }


def is_running() -> bool:
    return bool(_state["running"])


async def _fetch(url: str, destination: Path) -> int:
    written = 0
    temp = destination.with_name(destination.name + ".download")
    timeout = httpx.Timeout(30.0, read=180.0, write=60.0, pool=30.0)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with temp.open("wb") as handle:
                async for chunk in response.aiter_bytes(128 * 1024):
                    written += len(chunk)
                    if written > MAX_BYTES:
                        raise ValueError(f"文件超过 {MAX_BYTES // 1024 // 1024}MB 上限，已中止")
                    handle.write(chunk)
                    _state["bytes"] = written
    try:
        database_type = _database_type(temp)
    except Exception as exc:
        temp.unlink(missing_ok=True)
        raise ValueError(f"下载内容不是有效的 mmdb 数据库（{type(exc).__name__}）") from exc
    if written < 1024:
        temp.unlink(missing_ok=True)
        raise ValueError("下载内容过小，可能被拦截或返回了错误页")
    if destination.exists():
        destination.unlink()
    os.replace(temp, destination)
    _state["database_type"] = database_type
    return written


async def _run(kind: str, url: str) -> None:
    _state.update({"running": True, "kind": kind, "url": url, "ok": None, "message": f"正在下载 {kind} 数据库…", "bytes": 0, "started_at": time.time()})
    try:
        destination = target_path(kind)
        size = await _fetch(url, destination)
        _state.update(
            {
                "ok": True,
                "message": f"下载完成：{kind} → {destination}（{size / 1024 / 1024:.1f}MB，{_state.get('database_type', '')}）",
                "path": str(destination),
            }
        )
        from . import geo

        geo.clear_reader_cache()
        if str(destination) != runtime_config.geoip_path(kind):
            await runtime_config.update(
                {"geoip_db_path" if kind == "country" else "geoip_city_db_path": str(destination)}
            )
    except Exception as exc:
        _state.update({"ok": False, "message": f"下载失败：{type(exc).__name__} {exc}"[:300]})
    finally:
        _state["running"] = False
        _state["finished_at"] = time.time()


async def download(kind: str, url: str | None = None) -> dict[str, Any]:
    global _task
    if kind not in KINDS:
        raise ValueError("kind 只能是 country 或 city")
    target_url = (url or runtime_config.geoip_url(kind) or "").strip() or runtime_config.geoip_url(kind)
    if not target_url.startswith(("http://", "https://")):
        raise ValueError("下载地址必须以 http:// 或 https:// 开头")
    if is_running():
        return {"started": False, "message": "已有下载任务在进行中", "download": dict(_state)}
    async with _lock:
        if is_running():
            return {"started": False, "message": "已有下载任务在进行中", "download": dict(_state)}
        _task = asyncio.create_task(_run(kind, target_url))
    return {"started": True, "message": "已开始下载，请稍候刷新状态", "download": dict(_state)}


async def auto_download_missing() -> list[str]:
    started: list[str] = []
    for kind in KINDS:
        if not runtime_config.auto_download_geoip():
            break
        path = Path(runtime_config.geoip_path(kind))
        if path.exists() and path.stat().st_size > 1024:
            continue
        try:
            result = await download(kind)
        except ValueError:
            continue
        if result.get("started"):
            started.append(kind)
            deadline = time.time() + 900
            while is_running() and time.time() < deadline:
                await asyncio.sleep(1.0)
    return started
