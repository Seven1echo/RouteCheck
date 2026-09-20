from __future__ import annotations

import copy
import gzip
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "routecheck:v2026.9.16"
IMAGE_REPO = "routecheck"
IMAGE_TAG = "v2026.9.16"
APP_TITLE = "RouteCheck"
APP_VERSION = "V2026.9.16"
PROJECT_URL = "https://github.com/Seven1echo/RouteCheck"
BASE_REF = "3.12-slim-bookworm"
OUTPUT = ROOT / "routecheck.tar"


def request(url: str, token: str | None = None, accept: str | None = None) -> bytes:
    headers = {"User-Agent": "routecheck-image-export/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def add_tree(tar: tarfile.TarFile, source: Path, arc_prefix: str) -> None:
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source).as_posix()
        arcname = f"{arc_prefix.rstrip('/')}/{relative}" if relative else arc_prefix.rstrip("/")
        tar.add(item, arcname=arcname, recursive=False)


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="mihomo-image-", dir=ROOT))
    try:
        token_payload = json.loads(request("https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull"))
        token = token_payload["token"]
        registry = "https://registry-1.docker.io/v2/library/python"
        accept_index = ", ".join(
            [
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            ]
        )
        index = json.loads(request(f"{registry}/manifests/{BASE_REF}", token, accept_index))
        if "manifests" in index:
            selected = next(
                item for item in index["manifests"] if item.get("platform", {}).get("os") == "linux" and item.get("platform", {}).get("architecture") == "amd64"
            )
            manifest_ref = selected["digest"]
        else:
            manifest_ref = BASE_REF
        manifest = json.loads(request(f"{registry}/manifests/{manifest_ref}", token, "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"))

        layers: list[tuple[Path, Path]] = []
        layer_dir = work / "layers"
        layer_dir.mkdir()
        for index_number, descriptor in enumerate(manifest["layers"]):
            compressed = layer_dir / f"compressed-{index_number}.blob"
            uncompressed = layer_dir / f"layer-{index_number}.tar"
            compressed.write_bytes(request(f"{registry}/blobs/{descriptor['digest']}", token))
            with gzip.open(compressed, "rb") as source, uncompressed.open("wb") as target:
                shutil.copyfileobj(source, target)
            layers.append((compressed, uncompressed))

        config_blob = request(f"{registry}/blobs/{manifest['config']['digest']}", token)
        base_config = json.loads(config_blob)

        wheelhouse = work / "wheelhouse"
        wheelhouse.mkdir()
        subprocess.run(
            [
                "python",
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--platform",
                "manylinux_2_17_x86_64",
                "--python-version",
                "312",
                "--implementation",
                "cp",
                "--abi",
                "cp312",
                "--dest",
                str(wheelhouse),
                "-r",
                str(ROOT / "requirements.txt"),
            ],
            check=True,
        )

        extra_root = work / "extra-root"
        packages = extra_root / "usr/local/lib/python3.12/site-packages"
        packages.mkdir(parents=True)
        for wheel in wheelhouse.glob("*.whl"):
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(packages)

        extra_tar = work / "extra-layer.tar"
        with tarfile.open(extra_tar, "w") as tar:
            add_tree(tar, ROOT / "app", "app/app")
            add_tree(tar, packages, "usr/local/lib/python3.12/site-packages")
        extra_diff_id = "sha256:" + sha256(extra_tar)

        image_config = copy.deepcopy(base_config)
        image_config["created"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        image_config.setdefault("config", {})["WorkingDir"] = "/app"
        image_config["config"]["Cmd"] = ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
        image_config["config"]["ExposedPorts"] = {"8787/tcp": {}}
        image_config["config"]["Labels"] = {
            "org.opencontainers.image.title": APP_TITLE,
            "org.opencontainers.image.version": APP_VERSION,
            "org.opencontainers.image.source": PROJECT_URL,
            "org.opencontainers.image.description": "Mihomo 漏网之鱼采集、直连探测与直连规则生成器",
        }
        env = image_config["config"].setdefault("Env", [])
        env.extend([f"APP_NAME={APP_TITLE}", f"APP_VERSION={APP_VERSION}", "PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1"])
        image_config.setdefault("rootfs", {}).setdefault("diff_ids", []).append(extra_diff_id)
        image_config.setdefault("history", []).append({"created": image_config["created"], "created_by": f"{APP_TITLE} {APP_VERSION} local export"})
        config_bytes = json.dumps(image_config, separators=(",", ":")).encode("utf-8")
        config_name = hashlib.sha256(config_bytes).hexdigest() + ".json"

        archive_manifest = {
            "Config": config_name,
            "RepoTags": [IMAGE],
            "Layers": [f"layer-{number}/layer.tar" for number in range(len(layers))] + ["layer-extra/layer.tar"],
        }
        repositories = {IMAGE_REPO: {IMAGE_TAG: hashlib.sha256(config_bytes).hexdigest()}}
        with tarfile.open(OUTPUT, "w") as output:
            info = tarfile.TarInfo(config_name)
            info.size = len(config_bytes)
            output.addfile(info, __import__("io").BytesIO(config_bytes))
            manifest_bytes = json.dumps([archive_manifest], indent=2).encode("utf-8")
            info = tarfile.TarInfo("manifest.json")
            info.size = len(manifest_bytes)
            output.addfile(info, __import__("io").BytesIO(manifest_bytes))
            repo_bytes = json.dumps(repositories).encode("utf-8")
            info = tarfile.TarInfo("repositories")
            info.size = len(repo_bytes)
            output.addfile(info, __import__("io").BytesIO(repo_bytes))
            for number, (_, layer_tar) in enumerate(layers):
                output.add(layer_tar, f"layer-{number}/layer.tar")
            output.add(extra_tar, "layer-extra/layer.tar")
        print(f"exported={OUTPUT}")
        print(f"size_mb={OUTPUT.stat().st_size / 1024 / 1024:.1f}")
        print(f"image={IMAGE}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
