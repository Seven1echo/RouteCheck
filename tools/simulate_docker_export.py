from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
OUTPUT = Path(r"C:\Users\Administrator\Desktop\RouteCheck-simulated-image.tar")
IMAGE_TAG = "routecheck:simulated"


def add_project_files(layer: tarfile.TarFile) -> None:
    excluded_dirs = {".git", ".pytest_cache", "__pycache__", "data"}
    excluded_files = {"RouteCheck-simulated-image.tar"}
    for path in sorted(PROJECT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(PROJECT)
        if any(part in excluded_dirs for part in relative.parts):
            continue
        if path.name in excluded_files or path.suffix in {".pyc", ".mmdb"}:
            continue
        layer.add(path, arcname=Path("app") / relative)


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="routecheck-image-") as temp_dir:
        layer_path = Path(temp_dir) / "layer.tar"
        with tarfile.open(layer_path, "w") as layer:
            add_project_files(layer)

        layer_bytes = layer_path.read_bytes()
        layer_digest = hashlib.sha256(layer_bytes).hexdigest()
        created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        config = {
            "created": created,
            "architecture": "amd64",
            "os": "linux",
            "config": {
                "Env": ["PYTHONUNBUFFERED=1", "PYTHONDONTWRITEBYTECODE=1"],
                "WorkingDir": "/app",
                "Cmd": ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"],
                "ExposedPorts": {"8787/tcp": {}},
                "Labels": {
                    "org.opencontainers.image.title": "RouteCheck simulated image",
                    "org.opencontainers.image.description": "Generated without Docker engine",
                },
            },
            "rootfs": {"type": "layers", "diff_ids": [f"sha256:{layer_digest}"]},
            "history": [{"created": created, "created_by": "Python simulated export"}],
        }
        config_bytes = json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        config_name = hashlib.sha256(config_bytes).hexdigest() + ".json"
        manifest = [{"Config": config_name, "RepoTags": [IMAGE_TAG], "Layers": ["layer.tar"]}]

        with tarfile.open(OUTPUT, "w") as image:
            config_info = tarfile.TarInfo(config_name)
            config_info.size = len(config_bytes)
            image.addfile(config_info, fileobj=__import__("io").BytesIO(config_bytes))
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            manifest_info = tarfile.TarInfo("manifest.json")
            manifest_info.size = len(manifest_bytes)
            image.addfile(manifest_info, fileobj=__import__("io").BytesIO(manifest_bytes))
            image.add(layer_path, arcname="layer.tar")

    print(f"created: {OUTPUT}")
    print(f"tag: {IMAGE_TAG}")
    print(f"size: {OUTPUT.stat().st_size} bytes")


if __name__ == "__main__":
    main()
