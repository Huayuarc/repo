#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Huayuarc 源仓库工具（R2 中转发布）。

子命令：
  publish      校验 debs/ 里的中转 deb → 上传 R2 → 写 packages/ 与 registry.json → 清理中转文件
  index        由 packages/ 生成 Packages、Packages.gz、Release（站点根目录）
  verify-site  确认 Pages 已经把新的 Packages 发布出去
  sweep        下架保护期：最后引用消失 48 小时后，两次确认才删除 R2 文件
  mirror       从旧源（默认 https://huayuarc.github.io）拉取 deb 搬进 R2，一次迁移
  check        校验 packages/ 与 registry.json 的一致性（不需要网络）

publish / sweep / mirror 需要的环境变量：
  R2_ACCOUNT_ID  R2_ACCESS_KEY_ID  R2_SECRET_ACCESS_KEY  R2_BUCKET
  R2_PUBLIC_URL   例如 https://debs.example.com（结尾不写 /），也可写进 repo.json 的 publicBaseURL
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
DEBS = ROOT / "debs"
PACKAGES_DIR = ROOT / "packages"
REGISTRY = ROOT / "registry.json"
ORPHANS = ROOT / "orphans.json"
CONFIG = ROOT / "repo.json"
REPORT = ROOT / ".publish-report.json"

PROTECT_SECONDS = 48 * 3600
MIN_CONFIRMATIONS = 2

PKG_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]*$")
VER_RE = re.compile(r"^[0-9][0-9A-Za-z.+~:\-]*$")
ARCHES = {"iphoneos-arm64", "iphoneos-arm64e", "iphoneos-arm"}

FIELD_ORDER = [
    "Package", "Name", "Version", "Architecture", "Maintainer", "Author", "Section",
    "Priority", "Essential", "Installed-Size", "Depends", "Pre-Depends",
    "Recommends", "Suggests", "Conflicts", "Breaks", "Replaces", "Provides",
    "Homepage", "Icon", "Depiction", "SileoDepiction", "Description",
    "Filename", "Size", "MD5sum", "SHA1", "SHA256",
]


# ---------------------------------------------------------------- 基础工具

def die(msg: str, code: int = 1):
    print(f"✖ {msg}", file=sys.stderr)
    raise SystemExit(code)


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> float:
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def load_json(path: Path, default):
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{num} B"


def config() -> dict:
    data = load_json(CONFIG, {})
    return {
        "origin": data.get("origin", "Huayuarc"),
        "label": data.get("label", "Huayuarc Repo"),
        "description": data.get("description", "Huayuarc 插件源 · R2 直连"),
        "suite": data.get("suite", "stable"),
        "codename": data.get("codename", "ios"),
        "publicBaseURL": data.get("publicBaseURL", ""),
        "siteURL": data.get("siteURL", ""),
        "mirrorSource": data.get("mirrorSource", ""),
    }


def public_base() -> str:
    base = (os.environ.get("R2_PUBLIC_URL") or config()["publicBaseURL"] or "").strip().rstrip("/")
    if not base:
        die("缺少 R2_PUBLIC_URL：在仓库 Settings ▸ Secrets and variables ▸ Actions 里配置，或写进 repo.json 的 publicBaseURL")
    return base


def site_url() -> str:
    url = (os.environ.get("SITE_URL") or config()["siteURL"] or "").strip().rstrip("/")
    if url:
        return url
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        if name.lower() == f"{owner.lower()}.github.io":
            return f"https://{owner.lower()}.github.io"
        return f"https://{owner.lower()}.github.io/{name}"
    return ""


def mirror_source() -> str:
    src = (os.environ.get("MIRROR_SOURCE") or config()["mirrorSource"] or "").strip().rstrip("/")
    if not src:
        die("缺少旧源地址：写进 repo.json 的 mirrorSource，或给 mirror --source")
    return src


# ------------------------------------------------------------ deb 控制字段

def parse_control_text(text: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    key = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw[0] in " \t":
            if key:
                fields[key] += "\n" + raw.strip()
            continue
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip()
        fields[key] = value.strip()
    return fields


def ar_members(path: Path):
    data = path.read_bytes()
    if not data.startswith(b"!<arch>\n"):
        raise ValueError("不是 ar 归档")
    pos = 8
    while pos + 60 <= len(data):
        header = data[pos:pos + 60]
        name = header[0:16].decode("utf-8", "replace").strip()
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError:
            break
        start = pos + 60
        yield name.rstrip("/"), data[start:start + size]
        pos = start + size + (size % 2)


def read_control_pure(deb: Path) -> Dict[str, str]:
    for name, blob in ar_members(deb):
        if not name.startswith("control.tar"):
            continue
        with tarfile.open(fileobj=BytesIO(blob), mode="r:*") as tar:
            for member in tar.getmembers():
                if member.name.lstrip("./") == "control":
                    handle = tar.extractfile(member)
                    if handle is not None:
                        return parse_control_text(handle.read().decode("utf-8", "replace"))
    raise ValueError("没有找到 control.tar.* / control")


def read_control(deb: Path) -> Dict[str, str]:
    if shutil.which("dpkg-deb"):
        proc = subprocess.run(["dpkg-deb", "-f", str(deb)], capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            return parse_control_text(proc.stdout)
    return read_control_pure(deb)


# --------------------------------------------------------------- R2 客户端

def r2_client():
    names = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        die("缺少环境变量：" + "、".join(missing) + "（仓库 Settings ▸ Secrets and variables ▸ Actions 里配置）")
    try:
        import boto3  # noqa: WPS433
        from botocore.config import Config  # noqa: WPS433
    except ImportError:
        die("需要 boto3：python3 -m pip install boto3")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
    )


def bucket() -> str:
    return os.environ["R2_BUCKET"]


def remote_object(client, key: str) -> Optional[dict]:
    from botocore.exceptions import ClientError  # noqa: WPS433
    try:
        head = client.head_object(Bucket=bucket(), Key=key)
    except ClientError as err:
        code = str(err.response.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    meta = {k.lower(): v for k, v in (head.get("Metadata") or {}).items()}
    return {"size": head.get("ContentLength", 0), "sha256": meta.get("sha256", "")}


def remote_put(client, path: Path, key: str, sha: str) -> None:
    client.upload_file(
        str(path),
        bucket(),
        key,
        ExtraArgs={
            "ContentType": "application/vnd.debian.binary-package",
            "Metadata": {"sha256": sha},
        },
    )


def remote_delete(client, key: str) -> None:
    client.delete_object(Bucket=bucket(), Key=key)


# ------------------------------------------------------------- package 记录

def package_path(package: str) -> Path:
    return PACKAGES_DIR / f"{package}.json"


def load_package(package: str) -> dict:
    return load_json(package_path(package), {"package": package, "versions": []})


def all_packages() -> List[dict]:
    if not PACKAGES_DIR.exists():
        return []
    records = []
    for path in sorted(PACKAGES_DIR.glob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as err:
            die(f"{path.relative_to(ROOT)} 不是合法 JSON：{err}")
    return records


def referenced_urls(records: List[dict]) -> set:
    urls = set()
    for rec in records:
        for ver in rec.get("versions", []):
            if ver.get("url"):
                urls.add(ver["url"])
    return urls


def store_version(package: str, item: dict) -> None:
    """把一条版本记录写进 packages/<id>.json。"""
    record = load_package(package)
    record["package"] = package
    record["name"] = item["control"].get("Name", record.get("name", package))
    record["section"] = item["control"].get("Section", record.get("section", "Tweaks"))
    versions = [
        ver for ver in record.get("versions", [])
        if (ver.get("version"), ver.get("architecture")) != (item["version"], item["architecture"])
    ]
    versions.append({
        "version": item["version"],
        "architecture": item["architecture"],
        "file": item["key"],
        "url": item["url"],
        "size": item["size"],
        "sha256": item["sha256"],
        "uploadedAt": item["uploadedAt"],
        "control": item["control"],
    })
    record["versions"] = versions
    record["updatedAt"] = item["uploadedAt"]
    save_json(package_path(package), record)


def referenced_keys(registry: Dict[str, dict], records: List[dict]) -> set:
    urls = referenced_urls(records)
    return {key for key, entry in registry.items() if entry.get("url") in urls or entry.get("file") == key}


def version_key(version: str):
    parts = []
    for chunk in re.split(r"(\d+)", version):
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk))
    return parts


# ----------------------------------------------------------------- publish

def cmd_publish(args) -> None:
    relay = sorted(DEBS.glob("*.deb")) if DEBS.exists() else []
    if not relay:
        print("· 没有待处理的中转 deb")
        return

    registry = load_json(REGISTRY, {})
    orphans = load_json(ORPHANS, {})
    staged = []
    errors: List[str] = []

    for deb in relay:
        try:
            control = read_control(deb)
        except Exception as err:  # noqa: BLE001
            errors.append(f"{deb.name}：读不到控制文件（{err}）")
            continue

        missing = [f for f in ("Package", "Name", "Version", "Architecture", "Description", "Maintainer")
                   if not control.get(f)]
        if missing:
            errors.append(f"{deb.name}：控制文件缺少 {'、'.join(missing)}")
            continue
        if not control.get("Section"):
            print(f"⚠ {deb.name}：没有 Section，按 Tweaks 归档")
            control["Section"] = "Tweaks"
        if not control.get("Author"):
            control["Author"] = control["Maintainer"]

        package = control["Package"].strip()
        version = control["Version"].strip()
        arch = control["Architecture"].strip()
        if not PKG_RE.match(package):
            errors.append(f"{deb.name}：Package「{package}」不是合法包标识")
            continue
        if not VER_RE.match(version):
            errors.append(f"{deb.name}：Version「{version}」不是合法版本号")
            continue
        if arch not in ARCHES:
            errors.append(f"{deb.name}：架构 {arch} 不支持（只收 {' / '.join(sorted(ARCHES))}）")
            continue
        if deb.stat().st_size == 0:
            errors.append(f"{deb.name}：文件是空的")
            continue

        filename = f"{package}_{version}_{arch}.deb"
        key = f"debs/{filename}"
        sha = sha256_of(deb)
        size = deb.stat().st_size
        existing = registry.get(key)
        if existing and existing.get("sha256") and existing["sha256"] != sha:
            errors.append(f"{key} 已经发布过、内容却不同：请提升版本号再上传（同名文件不允许覆盖）")
            continue
        if existing and existing.get("sha256") == sha:
            print(f"↻ {filename} 已发布且内容相同，跳过重复上传")

        staged.append({
            "deb": deb, "package": package, "version": version, "architecture": arch,
            "key": key, "sha256": sha, "size": size,
            "control": {k: v for k, v in control.items()
                        if k not in ("Filename", "Size", "SHA256", "SHA1", "MD5sum")},
            "duplicate": bool(existing and existing.get("sha256") == sha),
        })

    if errors:
        for line in errors:
            print(f"✖ {line}", file=sys.stderr)
        die("有 deb 未通过校验：本次一个都不发布，中转文件全部保留")

    if args.dry_run:
        for item in staged:
            print(f"· [dry-run] {item['deb'].name} → debs/{item['package']}_{item['version']}_{item['architecture']}.deb"
                  f"（{human_size(item['size'])}）")
        print(f"· [dry-run] 共 {len(staged)} 个，未做任何改动")
        return

    base = public_base()
    client = r2_client()
    report = []

    for item in staged:
        key = item["key"]
        if not item["duplicate"]:
            remote = remote_object(client, key)
            if remote and remote.get("sha256") == item["sha256"]:
                print(f"↻ R2 已有相同文件，跳过上传：{key}")
            else:
                if remote:
                    print(f"⚠ R2 上 {key} 内容不同，将被这次覆盖")
                print(f"↑ 上传 {key}（{human_size(item['size'])}）")
                remote_put(client, item["deb"], key, item["sha256"])

        url = f"{base}/{key}"
        entry = {
            "package": item["package"], "version": item["version"],
            "architecture": item["architecture"], "file": key, "url": url,
            "size": item["size"], "sha256": item["sha256"], "uploadedAt": iso_now(),
        }
        registry[key] = entry
        orphans.pop(key, None)
        item["url"] = url
        item["uploadedAt"] = entry["uploadedAt"]
        store_version(item["package"], item)
        report.append({
            "package": item["package"], "version": item["version"],
            "architecture": item["architecture"], "url": url,
        })

    bad = verify_urls([item["url"] for item in report])
    if bad:
        die("R2 直链确认下载失败：" + "、".join(bad) + "；中转文件保留，请检查 R2 公开域名")

    for item in staged:
        item["deb"].unlink()
        print(f"🗑 已从工作区移除中转文件 debs/{item['deb'].name}（提交后 GitHub 上才消失）")

    save_json(REGISTRY, registry)
    save_json(ORPHANS, orphans)
    save_json(REPORT, report)
    print(f"✔ 发布完成：{len(staged)} 个 deb 已进 R2，记录写入 packages/ 与 registry.json")


def verify_urls(urls: List[str], attempts: int = 5, delay: int = 3) -> List[str]:
    bad = []
    for url in urls:
        ok = False
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req, timeout=20) as resp:
                    if resp.status == 200:
                        ok = True
                        break
            except Exception:  # noqa: BLE001
                time.sleep(delay * (attempt + 1))
        if ok:
            print(f"✔ 可下载：{url}")
        else:
            bad.append(url)
    return bad


# ------------------------------------------------------------------ mirror

def fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "huayuarc-repo-mirror"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def split_stanzas(text: str) -> List[str]:
    blocks, current = [], []
    for line in text.splitlines():
        if line.strip():
            current.append(line)
        elif current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


def cmd_mirror(args) -> None:
    source = (args.source or mirror_source()).rstrip("/")
    print(f"· 旧源：{source}")
    raw = fetch_bytes(f"{source}/Packages")
    stanzas = [parse_control_text(block) for block in split_stanzas(raw.decode("utf-8", "replace"))]
    stanzas = [s for s in stanzas if s.get("Package") and s.get("Filename")]
    print(f"· 旧索引共 {len(stanzas)} 条")

    if not args.all:
        newest: Dict[tuple, dict] = {}
        for stanza in stanzas:
            ident = (stanza["Package"], stanza.get("Architecture", ""))
            current = newest.get(ident)
            if current is None or version_key(stanza.get("Version", "")) > version_key(current.get("Version", "")):
                newest[ident] = stanza
        stanzas = list(newest.values())
        print(f"· 只搬每个插件/架构的最新版：{len(stanzas)} 条")

    registry = load_json(REGISTRY, {})
    base = (os.environ.get("R2_PUBLIC_URL") or config()["publicBaseURL"] or "").rstrip("/")

    pending = []
    for stanza in stanzas:
        filename = stanza["Filename"]
        if base and filename.startswith(base):
            continue
        name = filename.rsplit("/", 1)[-1]
        key = f"debs/{name}"
        sha = stanza.get("SHA256", "").lower()
        existing = registry.get(key)
        if existing and (not sha or existing.get("sha256") == sha):
            continue
        pending.append((stanza, key, sha, filename))

    if args.limit:
        pending = pending[:args.limit]
    print(f"· 待迁移 {len(pending)} 个文件")

    if args.dry_run:
        for stanza, key, _sha, _filename in pending[:20]:
            print(f"· [dry-run] {stanza['Package']} {stanza.get('Version')} → {key}")
        print(f"· [dry-run] 未做任何改动")
        return

    base = public_base()
    client = r2_client()
    done = failed = 0
    report = []
    with tempfile.TemporaryDirectory() as tmp:
        for stanza, key, sha, filename in pending:
            package = stanza["Package"]
            version = stanza.get("Version", "")
            arch = stanza.get("Architecture", "")
            url = filename if filename.startswith("http") else f"{source}/{filename.lstrip('/')}"
            target = Path(tmp) / os.path.basename(key)
            try:
                print(f"↓ {package} {version} …")
                with urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": "huayuarc-repo-mirror"}),
                    timeout=120,
                ) as resp, target.open("wb") as out:
                    shutil.copyfileobj(resp, out, 1 << 20)
            except Exception as err:  # noqa: BLE001
                print(f"✖ 下载失败 {url}：{err}", file=sys.stderr)
                failed += 1
                continue

            actual = sha256_of(target)
            if sha and actual != sha:
                print(f"✖ 校验不符 {url}：索引 {sha[:12]}… 实际 {actual[:12]}…", file=sys.stderr)
                failed += 1
                target.unlink(missing_ok=True)
                continue

            if arch not in ARCHES:
                print(f"· 跳过非常规架构 {arch}（{package}）")
                target.unlink(missing_ok=True)
                continue

            remote = remote_object(client, key)
            if not (remote and remote.get("sha256") == actual):
                remote_put(client, target, key, actual)
            target.unlink(missing_ok=True)

            public = f"{base}/{key}"
            entry = {
                "package": package, "version": version, "architecture": arch,
                "file": key, "url": public, "size": int(stanza.get("Size") or 0),
                "sha256": actual, "uploadedAt": iso_now(),
            }
            registry[key] = entry
            store_version(package, {
                "version": version, "architecture": arch, "key": key, "url": public,
                "size": entry["size"], "sha256": actual, "uploadedAt": entry["uploadedAt"],
                "control": {k: v for k, v in stanza.items()
                            if k not in ("Filename", "Size", "SHA256", "SHA1", "MD5sum")},
            })
            report.append({"package": package, "version": version, "architecture": arch, "url": public})
            done += 1

    save_json(REGISTRY, registry)
    save_json(REPORT, report)
    print(f"✔ 迁移 {done} 个，失败 {failed} 个")
    if failed and not done:
        die("一个都没搬成功，检查旧源地址或权限")


# ------------------------------------------------------------------- index

def format_stanza(control: Dict[str, str]) -> str:
    lines: List[str] = []
    done = set()
    order = FIELD_ORDER + [k for k in sorted(control) if k not in FIELD_ORDER]
    for field in order:
        if field in done or field not in control:
            continue
        done.add(field)
        parts = str(control[field]).strip().split("\n")
        lines.append(f"{field}: {parts[0].strip()}")
        for extra in parts[1:]:
            extra = extra.strip()
            lines.append(" ." if not extra else f" {extra}")
    return "\n".join(lines)


def build_packages_text() -> tuple:
    entries = []
    records = all_packages()
    for record in records:
        for ver in record.get("versions", []):
            control = dict(ver.get("control", {}))
            control["Package"] = record["package"]
            control["Version"] = ver["version"]
            control["Architecture"] = ver["architecture"]
            control["Filename"] = ver["url"]
            control["Size"] = str(ver["size"])
            control["SHA256"] = ver["sha256"]
            control.setdefault("Section", record.get("section", "Tweaks"))
            entries.append(control)
    entries.sort(key=lambda item: (item["Package"].lower(), version_key(item["Version"])))
    text = "".join(format_stanza(entry) + "\n\n" for entry in entries)
    return text, entries, records


def cmd_index(args) -> None:
    text, entries, records = build_packages_text()
    data = text.encode("utf-8")

    (ROOT / "Packages").write_bytes(data)
    with (ROOT / "Packages.gz").open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            gz.write(data)

    packages_gz = (ROOT / "Packages.gz").read_bytes()
    cfg = config()
    arches = sorted({entry["Architecture"] for entry in entries}) or sorted(ARCHES)
    date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S UTC")

    lines = [
        f"Origin: {cfg['origin']}",
        f"Label: {cfg['label']}",
        f"Suite: {cfg['suite']}",
        "Version: 1.0",
        f"Codename: {cfg['codename']}",
        f"Architectures: {' '.join(arches)}",
        "Components: main",
        f"Description: {cfg['description']}",
        f"Date: {date}",
        "NotAutomatic: No",
        "",
    ]
    sums = [
        ("MD5Sum", hashlib.md5),
        ("SHA1", hashlib.sha1),
        ("SHA256", hashlib.sha256),
    ]
    for name, algo in sums:
        lines.append(f"{name}:")
        lines.append(f" {algo(data).hexdigest()} {len(data)} Packages")
        lines.append(f" {algo(packages_gz).hexdigest()} {len(packages_gz)} Packages.gz")
    (ROOT / "Release").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (ROOT / ".nojekyll").write_text("", encoding="utf-8")

    print(f"✔ 索引已重建：{len(records)} 个插件、{len(entries)} 个版本")


# ------------------------------------------------------------------- sweep

def cmd_sweep(args) -> None:
    registry = load_json(REGISTRY, {})
    orphans = load_json(ORPHANS, {})
    live = referenced_keys(registry, all_packages())

    client = None if args.dry_run else r2_client()
    now = time.time()
    changed = False
    removed = []

    for key in list(registry):
        if key in live:
            if key in orphans:
                orphans.pop(key, None)
                changed = True
            continue

        entry = orphans.get(key)
        if not entry:
            orphans[key] = {
                "package": registry[key].get("package"),
                "version": registry[key].get("version"),
                "architecture": registry[key].get("architecture"),
                "url": registry[key].get("url"),
                "firstSeenAt": iso_now(),
                "lastSeenAt": iso_now(),
                "confirmations": 1,
            }
            changed = True
            print(f"⏳ 失去引用，进入 48 小时保护期：{key}")
            continue

        entry["confirmations"] = int(entry.get("confirmations", 0)) + 1
        entry["lastSeenAt"] = iso_now()
        changed = True

        age = now - parse_iso(entry.get("firstSeenAt", ""))
        if age >= PROTECT_SECONDS and entry["confirmations"] >= MIN_CONFIRMATIONS:
            if args.dry_run:
                print(f"· [dry-run] 会删除 {key}（已保护 {age / 3600:.1f} 小时，确认 {entry['confirmations']} 次）")
                continue
            print(f"🗑 保护期满，删除 R2 文件：{key}")
            remote_delete(client, key)
            registry.pop(key, None)
            orphans.pop(key, None)
            removed.append(key)
        else:
            print(f"· 保护中 {key}：{age / 3600:.1f} / 48 小时，确认 {entry['confirmations']} 次")

    if args.dry_run:
        print("· [dry-run] 未做任何改动")
        return
    if changed:
        save_json(REGISTRY, registry)
        save_json(ORPHANS, orphans)
    print(f"✔ 巡检完成：清理 {len(removed)} 个文件，{len(orphans)} 个仍在保护期")


# ------------------------------------------------------------------ 其它

def cmd_verify_site(args) -> None:
    base = site_url()
    if not base:
        die("无法确定站点地址：设置 SITE_URL，或在 GitHub Actions 里运行")
    report = load_json(REPORT, [])
    if not report:
        print("· 没有本次发布记录，跳过站点确认")
        return

    for attempt in range(args.wait):
        try:
            text = fetch_bytes(f"{base}/Packages").decode("utf-8", "replace")
            release_ok = True
        except Exception:  # noqa: BLE001
            text, release_ok = "", False
        if release_ok:
            missing = [item["package"] for item in report
                       if f"Package: {item['package']}" not in text or item["url"] not in text]
            if not missing:
                print(f"✔ 站点索引已包含本次发布的全部 {len(report)} 个包")
                return
            print(f"· 索引还没更新（缺 {len(missing)} 个），稍后重试…")
        else:
            print("· 站点暂时无法访问，稍后重试…")
        if attempt < args.wait - 1:
            time.sleep(20)
    die(f"站点 {base} 还没有更新到本次发布：到 GitHub 的 Actions 标签看 Pages 部署是否失败")


def cmd_check(args) -> None:
    problems: List[str] = []
    seen = set()
    records = all_packages()
    for record in records:
        package = record.get("package")
        if not package:
            problems.append("有 packages/*.json 缺少 package 字段")
            continue
        if not record.get("versions"):
            problems.append(f"{package}：没有任何版本")
        for ver in record.get("versions", []):
            for field in ("version", "architecture", "url", "sha256", "size"):
                if not ver.get(field):
                    problems.append(f"{package}：版本记录缺少 {field}")
            ident = (package, ver.get("version"), ver.get("architecture"))
            if ident in seen:
                problems.append(f"{package}：{ident[1]}/{ident[2]} 重复")
            seen.add(ident)
    registry = load_json(REGISTRY, {})
    for key, entry in registry.items():
        if not entry.get("sha256"):
            problems.append(f"registry：{key} 缺少 sha256")
    if problems:
        for line in problems:
            print(f"✖ {line}", file=sys.stderr)
        die(f"共 {len(problems)} 处问题")
    print(f"✔ 记录一致：{len(records)} 个插件、{len(seen)} 个版本")


def main() -> None:
    parser = argparse.ArgumentParser(description="Huayuarc 源仓库工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_publish = sub.add_parser("publish", help="发布 debs/ 里的中转 deb")
    p_publish.add_argument("--dry-run", action="store_true")
    p_publish.set_defaults(func=cmd_publish)

    p_index = sub.add_parser("index", help="重建 Packages / Packages.gz / Release")
    p_index.set_defaults(func=cmd_index)

    p_sweep = sub.add_parser("sweep", help="下架保护期巡检")
    p_sweep.add_argument("--dry-run", action="store_true")
    p_sweep.set_defaults(func=cmd_sweep)

    p_verify = sub.add_parser("verify-site", help="确认站点已更新")
    p_verify.add_argument("--wait", type=int, default=6)
    p_verify.set_defaults(func=cmd_verify_site)

    p_mirror = sub.add_parser("mirror", help="把旧源的 deb 搬进 R2")
    p_mirror.add_argument("--source", default="")
    p_mirror.add_argument("--all", action="store_true", help="搬所有版本（默认只搬最新）")
    p_mirror.add_argument("--limit", type=int, default=0)
    p_mirror.add_argument("--dry-run", action="store_true")
    p_mirror.set_defaults(func=cmd_mirror)

    p_check = sub.add_parser("check", help="校验记录一致性")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
