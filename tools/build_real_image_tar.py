from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
OUTPUT = Path(r"C:\Users\Administrator\Desktop\RouteCheck-V2026.10.10.tar")
IMAGE_TAG = "routecheck:V2026.10.10"
BASE_IMAGE = "python:3.12-slim"
REGISTRY = "https://registry-1.docker.io"


def http_json(url: str, headers: dict[str, str] | None = None) -> dict:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def registry_token() -> str:
    query = urllib.parse.urlencode({"service": "registry.docker.io", "scope": "repository:library/python:pull"})
    payload = http_json(f"https://auth.docker.io/token?{query}")
    return str(payload["token"])


def get_base_manifest() -> tuple[dict, str, str]:
    token = registry_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": ", ".join(
            [
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            ]
        ),
    }
    tag_url = f"{REGISTRY}/v2/library/python/manifests/3.12-slim"
    index = http_json(tag_url, headers)
    if "manifests" in index:
        selected = next(
            item
            for item in index["manifests"]
            if (item.get("platform") or {}).get("os") == "linux"
            and (item.get("platform") or {}).get("architecture") == "amd64"
        )
        digest = str(selected["digest"])
        manifest = http_json(
            f"{REGISTRY}/v2/library/python/manifests/{digest}",
            {**headers, "Accept": selected["mediaType"]},
        )
    else:
        digest = ""
        manifest = index
    return manifest, token, digest


def download_blob(token: str, digest: str, destination: Path) -> None:
    url = f"{REGISTRY}/v2/library/python/blobs/{digest}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=180) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle, length=1024 * 1024)


def unpack_layer(blob: Path, destination: Path) -> str:
    with blob.open("rb") as source:
        magic = source.read(2)
    if magic == b"\x1f\x8b":
        reader = gzip.open(blob, "rb")
    else:
        reader = blob.open("rb")
    digest = hashlib.sha256()
    with reader, destination.open("wb") as output:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            output.write(chunk)
    return digest.hexdigest()


def download_dependencies(destination: Path) -> None:
    wheel_dir = destination.parent / "wheels"
    wheel_dir.mkdir()
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--platform",
        "manylinux_2_17_x86_64",
        "--python-version",
        "3.12",
        "--implementation",
        "cp",
        "--abi",
        "cp312",
        "--dest",
        str(wheel_dir),
        "-r",
        str(PROJECT / "requirements.txt"),
    ]
    subprocess.run(command, check=True)
    site_packages = destination / "usr/local/lib/python3.12/site-packages"
    site_packages.mkdir(parents=True)
    for wheel in sorted(wheel_dir.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                parts = Path(member.filename).parts
                if any(part == ".." for part in parts):
                    continue
                target_parts = parts
                if len(parts) >= 3 and parts[1] == ".data" and parts[0].endswith(".dist-info"):
                    data_type = parts[2]
                    if data_type not in {"purelib", "platlib"}:
                        continue
                    target_parts = parts[3:]
                target = site_packages.joinpath(*target_parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))


def add_application_layer(layer_path: Path, staging: Path) -> str:
    with tarfile.open(layer_path, "w") as layer:
        for path in sorted(PROJECT.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(PROJECT)
            if any(part in {".git", ".pytest_cache", "__pycache__", "data"} for part in relative.parts):
                continue
            if path.suffix in {".pyc", ".mmdb"}:
                continue
            layer.add(path, arcname=Path("app") / relative)
        layer.add(staging / "usr", arcname="usr")
    digest = hashlib.sha256(layer_path.read_bytes()).hexdigest()
    return digest


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="routecheck-real-image-") as temp:
        temp_dir = Path(temp)
        print(f"pulling base image: {BASE_IMAGE}", flush=True)
        manifest, token, _ = get_base_manifest()
        config_blob = temp_dir / "base-config.json"
        download_blob(token, str(manifest["config"]["digest"]), config_blob)
        base_config = json.loads(config_blob.read_text(encoding="utf-8"))

        layer_paths: list[Path] = []
        diff_ids: list[str] = []
        for index, descriptor in enumerate(manifest["layers"]):
            compressed = temp_dir / f"base-{index}.blob"
            unpacked = temp_dir / f"base-{index}.tar"
            print(f"downloading base layer {index + 1}/{len(manifest['layers'])}", flush=True)
            download_blob(token, str(descriptor["digest"]), compressed)
            diff_id = unpack_layer(compressed, unpacked)
            layer_paths.append(unpacked)
            diff_ids.append(f"sha256:{diff_id}")

        staging = temp_dir / "staging"
        download_dependencies(staging)
        app_layer = temp_dir / "app.tar"
        app_diff_id = add_application_layer(app_layer, staging)
        layer_paths.append(app_layer)
        diff_ids.append(f"sha256:{app_diff_id}")

        created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        image_config = dict(base_config)
        image_config["created"] = created
        image_config["architecture"] = "amd64"
        image_config["os"] = "linux"
        runtime = dict(image_config.get("config") or {})
        env = list(runtime.get("Env") or [])
        env = [item for item in env if not item.startswith("APP_VERSION=")]
        env.extend(["APP_VERSION=V2026.10.10", "PYTHONUNBUFFERED=1", "PYTHONDONTWRITEBYTECODE=1"])
        runtime.update(
            {
                "Env": env,
                "WorkingDir": "/app",
                "Cmd": ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"],
                "ExposedPorts": {"8787/tcp": {}},
            }
        )
        image_config["config"] = runtime
        image_config["rootfs"] = {"type": "layers", "diff_ids": diff_ids}
        history = list(image_config.get("history") or [])
        history.append({"created": created, "created_by": "RouteCheck V2026.10.10 application layer"})
        image_config["history"] = history
        config_bytes = json.dumps(image_config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        config_name = hashlib.sha256(config_bytes).hexdigest() + ".json"
        layer_names = [f"layer-{index:03d}.tar" for index in range(len(layer_paths))]
        manifest_json = [{"Config": config_name, "RepoTags": [IMAGE_TAG], "Layers": layer_names}]

        print(f"writing image: {OUTPUT}", flush=True)
        with tarfile.open(OUTPUT, "w") as image:
            config_info = tarfile.TarInfo(config_name)
            config_info.size = len(config_bytes)
            image.addfile(config_info, io.BytesIO(config_bytes))
            manifest_bytes = json.dumps(manifest_json, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            manifest_info = tarfile.TarInfo("manifest.json")
            manifest_info.size = len(manifest_bytes)
            image.addfile(manifest_info, io.BytesIO(manifest_bytes))
            for layer_path, layer_name in zip(layer_paths, layer_names):
                image.add(layer_path, arcname=layer_name)
    print(f"created: {OUTPUT}")
    print(f"tag: {IMAGE_TAG}")
    print(f"size: {OUTPUT.stat().st_size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
