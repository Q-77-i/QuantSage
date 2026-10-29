"""QuantSage 备份/恢复（P4-S6）。

两个入口：`scripts/backup.sh` / `scripts/restore.sh`；本文件是唯一实现。
只依赖标准库与宿主已有的 `docker compose` / `rsync` / `curl`。

口径（SPEC §7.3）：
* 默认落点 `~/QuantSageBackups`；`--target` 可指外置盘，未挂载即失败，不静默跳过。
* PG `pg_dump -Fc`（业务库 + langfuse）；Qdrant snapshot；`data/` 硬链接快照。
* 保留 7 日 + 4 周；周备用硬链接，删日备不丢周备。
* 恢复默认进 scratch 目标（新库/新集合/临时目录），覆盖线上需 `--force`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = Path.home() / "QuantSageBackups"
MIN_FREE_GB = 20
CHUNK = 1024 * 1024
DB_NAME = re.compile(r"^[A-Za-z0-9_]+$")


class Failure(RuntimeError):
    """一步失败——调用方决定是继续下一步还是整体退出。"""


def log(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"WARN: {message}", file=sys.stderr, flush=True)


def load_env() -> dict[str, str]:
    """只解析 `.env` 的 KEY=VALUE，不执行任何内容。"""
    path = ROOT / ".env"
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def cfg(env: dict[str, str], name: str, default: str) -> str:
    return os.environ.get(name) or env.get(name) or default


def run(cmd: list[str], *, cwd: Path = ROOT) -> str:
    proc = subprocess.run(cmd, check=False, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failure(f"{' '.join(cmd)} 失败（{proc.returncode}）：{proc.stderr.strip()[:500]}")
    return proc.stdout


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def http_json(url: str, method: str = "GET", payload: Any = None, timeout: float = 60.0) -> Any:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"} if data else {}
    req = request.Request(url, data=data, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        raise Failure(f"{method} {url} → {exc.code}：{body}") from exc
    except error.URLError as exc:
        raise Failure(f"{method} {url} 不可达：{exc.reason}") from exc


def compose(args: list[str]) -> str:
    return run(["docker", "compose", *args])


def db_exists(user: str, db: str) -> bool:
    if not DB_NAME.match(db):
        raise Failure(f"非法库名：{db!r}")
    out = compose(
        [
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            user,
            "-d",
            "postgres",
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname = '{db}'",
        ]
    )
    return out.strip() == "1"


def git_sha() -> str:
    try:
        return run(["git", "rev-parse", "--short", "HEAD"]).strip()
    except Failure:
        return "unknown"


def latest_manifest(target: Path) -> dict[str, Any] | None:
    files = sorted((target / "manifests").glob("20*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ── 备份 ───────────────────────────────────────────────────────


def preflight(target: Path, *, need_pg: bool, need_qdrant: bool, dry: bool) -> None:
    # 外置盘未挂载时不能 `mkdir -p`——那会在内置盘上造一个同名目录，静默写错地方
    if target.is_absolute() and len(target.parts) > 2 and target.parts[:2] == ("/", "Volumes"):
        volume = Path("/Volumes") / target.parts[2]
        if not volume.is_dir():
            raise Failure(f"外置盘未挂载：{volume}")
    try:
        if not dry:
            for sub in ("pg", "qdrant", "data", "manifests", "logs", "weekly"):
                (target / sub).mkdir(parents=True, exist_ok=True)
            probe = target / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        anchor = target if target.exists() else target.parent
        free_gb = shutil.disk_usage(anchor).free / 1024**3
    except OSError as exc:
        raise Failure(f"备份目标不可写或不可用：{target}（{exc}）") from exc
    if free_gb < MIN_FREE_GB:
        raise Failure(f"目标剩余空间 {free_gb:.1f}GB < {MIN_FREE_GB}GB：{target}")
    if need_pg:
        compose(["exec", "-T", "postgres", "pg_isready", "-U", cfg(load_env(), "POSTGRES_USER", "quantsage"), "-d", cfg(load_env(), "POSTGRES_DB", "quantsage")])
    if need_qdrant:
        http_json(f"{qdrant_url()}/collections/{collection()}")


def qdrant_url() -> str:
    return cfg(load_env(), "QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")


def collection() -> str:
    return cfg(load_env(), "RAG_COLLECTION", "cn_events_v1")


def backup_pg(target: Path, date_str: str, dry: bool) -> dict[str, Any]:
    env = load_env()
    user = cfg(env, "POSTGRES_USER", "quantsage")
    primary = cfg(env, "POSTGRES_DB", "quantsage")
    entries: dict[str, Any] = {}
    for db in (primary, "langfuse"):
        if not db_exists(user, db):
            log(f"PG {db}: 不存在，跳过")
            continue
        dest = target / "pg" / f"{db}-{date_str}.dump"
        if dry:
            log(f"[dry] pg_dump {db} → {dest}")
            continue
        tmp = dest.with_name(dest.name + ".partial")
        with tmp.open("wb") as handle:
            proc = subprocess.run(
                ["docker", "compose", "exec", "-T", "postgres", "pg_dump", "-U", user, "-Fc", "--no-owner", db],
                check=False,
                cwd=ROOT,
                stdout=handle,
                stderr=subprocess.PIPE,
            )
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise Failure(f"pg_dump {db} 失败：{proc.stderr.decode(errors='replace')[:500]}")
        os.replace(tmp, dest)
        entries[db] = {
            "file": str(dest.relative_to(target)),
            "bytes": dest.stat().st_size,
            "sha256": sha256_file(dest),
        }
        log(f"PG {db}: {dest}（{entries[db]['bytes']} bytes）")
    return entries


def backup_qdrant(target: Path, date_str: str, data_dir: Path, dry: bool) -> dict[str, Any]:
    source = data_dir / "_meta" / "embeddings.json"
    source_sha = sha256_file(source) if source.is_file() else ""
    prev = latest_manifest(target)
    prev_q = (prev or {}).get("qdrant", {})
    dest = target / "qdrant" / f"{date_str}_{collection()}.snapshot"
    if source_sha and prev_q.get("file") and prev_q.get("source_sha") == source_sha:
        prev_file = target / prev_q["file"]
        if prev_file.is_file():
            if dry:
                log(f"[dry] Qdrant 复用 {prev_file}")
            elif dest.resolve() != prev_file.resolve():
                if dest.exists():
                    dest.unlink()
                try:
                    os.link(prev_file, dest)
                except OSError:
                    shutil.copy2(prev_file, dest)
                log(f"Qdrant: 复用 {prev_file}（embeddings 未变）")
            else:
                log("Qdrant: 复用当前快照（同日重跑）")
            return {
                "file": str(dest.relative_to(target)),
                "bytes": prev_q.get("bytes") or prev_file.stat().st_size,
                "sha256": prev_q.get("sha256", ""),
                "points_count": prev_q.get("points_count"),
                "source_sha": source_sha,
                "reused": True,
            }
    if dry:
        log(f"[dry] Qdrant snapshot {collection()} → {dest}")
        return {"file": str(dest.relative_to(target)), "reused": False, "source_sha": source_sha}
    info = http_json(f"{qdrant_url()}/collections/{collection()}")
    points = (info.get("result") or {}).get("points_count")
    response = http_json(
        f"{qdrant_url()}/collections/{collection()}/snapshots", method="POST", timeout=600.0
    )
    name = ((response.get("result") or {}).get("name")) or ""
    if not name:
        raise Failure(f"Qdrant snapshot 未返回 name：{response}")
    tmp = dest.with_name(dest.name + ".partial")
    # Qdrant 1.19 的落盘路径是 /qdrant/snapshots/<collection>/<name>，不是 /qdrant/snapshots/<name>
    container_path = f"/qdrant/snapshots/{collection()}/{name}"
    try:
        compose(["cp", f"qdrant:{container_path}", str(tmp)])
        os.replace(tmp, dest)
    finally:
        try:
            compose(["exec", "-T", "qdrant", "rm", "-f", container_path])
        except Failure:
            pass
    log(f"Qdrant: {dest}（{dest.stat().st_size} bytes，points={points}）")
    return {
        "file": str(dest.relative_to(target)),
        "bytes": dest.stat().st_size,
        "sha256": sha256_file(dest),
        "points_count": points,
        "source_sha": source_sha,
        "reused": False,
    }


def backup_data(target: Path, date_str: str, data_dir: Path, dry: bool) -> dict[str, Any]:
    dest = target / "data" / date_str
    prev_dirs = sorted((target / "data").glob("20*"))
    prev = prev_dirs[-1] if prev_dirs else None
    if dry:
        log(f"[dry] rsync {data_dir} → {dest}（link-dest={prev}）")
        return {"dir": str(dest.relative_to(target)), "dry": True}
    dest.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(dest / "_tmp", ignore_errors=True)
    cmd = ["rsync", "-a", "--exclude=_tmp/"]
    if prev is not None and prev != dest:
        cmd.append(f"--link-dest={prev}")
    cmd += [f"{data_dir}/", f"{dest}/"]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        warn("rsync --link-dest 失败，回退普通复制（目标 FS 可能不支持硬链接）")
        proc = subprocess.run(
            ["rsync", "-a", "--exclude=_tmp/", f"{data_dir}/", f"{dest}/"],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise Failure(f"rsync data 失败：{proc.stderr.strip()[:500]}")
    files = sorted(path for path in dest.rglob("*") if path.is_file())
    manifest = target / "manifests" / f"{date_str}.data.sha256"
    with manifest.open("w", encoding="utf-8") as handle:
        for path in files:
            rel = "./" + path.relative_to(dest).as_posix()
            handle.write(f"{sha256_file(path)}  {rel}\n")
    meta = {}
    for name in ("bars.json", "events.json"):
        path = dest / "_meta" / name
        if path.is_file():
            meta[name] = sha256_file(path)
    result = {
        "dir": str(dest.relative_to(target)),
        "files": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "sha256_manifest": str(manifest.relative_to(target)),
        "meta": meta,
    }
    log(f"data: {dest}（{result['files']} 文件 / {result['bytes']} bytes）")
    return result


def write_manifest(
    target: Path,
    date_str: str,
    sections: dict[str, Any],
    *,
    partial: bool,
    failures: list[str],
    dry: bool,
) -> Path:
    env = load_env()
    payload = {
        "date": date_str,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": git_sha(),
        "host": socket.gethostname(),
        "target": str(target),
        "partial": partial,
        "failures": failures,
        **sections,
        "config": {
            "api_workers": cfg(env, "API_WORKERS", "1"),
            "cache_enabled": cfg(env, "CACHE_ENABLED", "false"),
            "rag_collection": collection(),
            "events_default_window_months": cfg(env, "EVENTS_DEFAULT_WINDOW_MONTHS", "12"),
        },
    }
    path = target / "manifests" / f"{date_str}.json"
    if dry:
        log(f"[dry] manifest → {path}")
        return path
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    log(f"manifest: {path}")
    return path


def _link_or_copy(src: Path, dest: Path) -> None:
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def prune(target: Path, keep_daily: int, keep_weekly: int, dry: bool) -> None:
    manifests = sorted((target / "manifests").glob("20*.json"), key=lambda path: path.stem)
    dates = [path.stem for path in manifests]
    if not dates:
        return
    keep = set(dates[-keep_daily:])
    sundays = [d for d in dates if date.fromisoformat(d).weekday() == 6]
    weekly_keep = set(sundays[-keep_weekly:])
    weekly_root = target / "weekly"
    for day in sorted(weekly_keep):
        source = target / "data" / day
        week = date.fromisoformat(day).isocalendar()
        wdir = weekly_root / f"{week.year}-W{week.week:02d}"
        if dry:
            log(f"[dry] weekly {wdir}")
            continue
        if (wdir / "data").exists():
            continue
        wdir.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            try:
                shutil.copytree(source, wdir / "data", copy_function=os.link, dirs_exist_ok=True)
            except OSError:
                warn(f"weekly 硬链接失败，回退普通复制：{source}")
                shutil.copytree(source, wdir / "data", copy_function=shutil.copy2, dirs_exist_ok=True)
        for pattern in (f"pg/*-{day}.dump", f"qdrant/{day}_*.snapshot", f"manifests/{day}.json", f"manifests/{day}.data.sha256"):
            for item in target.glob(pattern):
                if item.is_file():
                    _link_or_copy(item, wdir / item.name)
    weeks = sorted(path for path in weekly_root.glob("*") if path.is_dir())
    for stale in weeks[:-keep_weekly] if keep_weekly else weeks:
        if dry:
            log(f"[dry] 删除周备 {stale}")
        else:
            shutil.rmtree(stale, ignore_errors=True)
    for day in sorted(set(dates) - keep):
        if dry:
            log(f"[dry] 删除日备 {day}")
            continue
        shutil.rmtree(target / "data" / day, ignore_errors=True)
        for pattern in (f"pg/*-{day}.dump", f"qdrant/{day}_*.snapshot", f"manifests/{day}.json", f"manifests/{day}.data.sha256"):
            for item in target.glob(pattern):
                item.unlink(missing_ok=True)


def do_backup(args: argparse.Namespace) -> int:
    env = load_env()
    target = Path(args.target or os.environ.get("QS_BACKUP_DIR") or DEFAULT_TARGET).expanduser().resolve()
    data_dir = Path(os.environ.get("DATA_DIR") or env.get("DATA_DIR") or ROOT / "data").expanduser()
    date_str = date.today().strftime("%Y%m%d")
    sections: dict[str, Any] = {"pg": {}, "qdrant": {}, "data": {}}
    # 同日重跑且用 --no-* 跳过某一步时，保留当天上一次的该段，避免把已备好的元数据抹掉
    today_manifest = target / "manifests" / f"{date_str}.json"
    if today_manifest.is_file():
        try:
            previous = json.loads(today_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
        for key, skip in (("pg", args.no_pg), ("qdrant", args.no_qdrant), ("data", args.no_data)):
            if skip and previous.get(key):
                sections[key] = previous[key]
    failures: list[str] = []
    log(f"备份目标：{target}；日期：{date_str}{'（dry-run）' if args.dry_run else ''}")
    try:
        preflight(target, need_pg=not args.no_pg, need_qdrant=not args.no_qdrant, dry=args.dry_run)
    except Failure as exc:
        warn(str(exc))
        return 2
    steps = []
    if not args.no_pg:
        steps.append(("pg", lambda: backup_pg(target, date_str, args.dry_run)))
    if not args.no_qdrant:
        steps.append(("qdrant", lambda: backup_qdrant(target, date_str, data_dir, args.dry_run)))
    if not args.no_data:
        steps.append(("data", lambda: backup_data(target, date_str, data_dir, args.dry_run)))
    for name, step in steps:
        try:
            sections[name] = step()
        except Failure as exc:
            warn(f"{name} 失败：{exc}")
            failures.append(f"{name}: {exc}")
    if not args.dry_run:
        write_manifest(target, date_str, sections, partial=bool(failures), failures=failures, dry=False)
        prune(target, args.keep_daily, args.keep_weekly, dry=False)
    else:
        write_manifest(target, date_str, sections, partial=bool(failures), failures=failures, dry=True)
    if failures:
        warn(f"完成但有 {len(failures)} 项失败；manifest 已标 partial=true")
        return 1
    log("备份完成")
    return 0


# ── 恢复 ───────────────────────────────────────────────────────


def restore_pg(target: Path, manifest: dict[str, Any], source_db: str, dest_db: str, force: bool) -> dict[str, Any]:
    entry = (manifest.get("pg") or {}).get(source_db)
    if not entry:
        raise Failure(f"manifest 里没有 {source_db} 的 PG 备份")
    if not DB_NAME.match(dest_db):
        raise Failure(f"非法目标库名：{dest_db!r}")
    user = cfg(load_env(), "POSTGRES_USER", "quantsage")
    if db_exists(user, dest_db):
        if not force:
            raise Failure(f"目标库 {dest_db} 已存在；覆盖需 --force")
        compose(["exec", "-T", "postgres", "dropdb", "-U", user, "--if-exists", dest_db])
    compose(["exec", "-T", "postgres", "createdb", "-U", user, dest_db])
    dump = target / entry["file"]
    if not dump.is_file():
        raise Failure(f"PG dump 不存在：{dump}")
    if entry.get("sha256") and sha256_file(dump) != entry["sha256"]:
        raise Failure(f"PG dump 校验失败：{dump}")
    try:
        with dump.open("rb") as handle:
            proc = subprocess.run(
                ["docker", "compose", "exec", "-T", "postgres", "pg_restore", "-U", user, "--no-owner", "-d", dest_db],
                check=False,
                cwd=ROOT,
                stdin=handle,
                capture_output=True,
            )
        if proc.returncode != 0:
            raise Failure(f"pg_restore 失败：{proc.stderr.decode(errors='replace')[:500]}")
        tables = compose(
            ["exec", "-T", "postgres", "psql", "-U", user, "-d", dest_db, "-tAc", "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"]
        ).strip()
        counts = {}
        for table in ("users", "strategies", "tasks", "research_reports"):
            out = compose(
                ["exec", "-T", "postgres", "psql", "-U", user, "-d", dest_db, "-tAc", f"SELECT to_regclass('public.{table}')"]
            ).strip()
            if out:
                counts[table] = compose(
                    ["exec", "-T", "postgres", "psql", "-U", user, "-d", dest_db, "-tAc", f"SELECT count(*) FROM {table}"]
                ).strip()
        return {"db": dest_db, "tables": int(tables), "counts": counts}
    except Exception:
        try:
            compose(["exec", "-T", "postgres", "dropdb", "-U", user, "--if-exists", dest_db])
        except Failure:
            pass
        raise


def restore_qdrant(target: Path, manifest: dict[str, Any], dest_collection: str, force: bool) -> dict[str, Any]:
    entry = manifest.get("qdrant") or {}
    if not entry.get("file"):
        raise Failure("manifest 里没有 Qdrant 备份")
    snapshot = target / entry["file"]
    if not snapshot.is_file():
        raise Failure(f"快照不存在：{snapshot}")
    if entry.get("sha256") and sha256_file(snapshot) != entry["sha256"]:
        raise Failure(f"Qdrant 快照校验失败：{snapshot}")
    base = qdrant_url()
    existing = None
    try:
        existing = http_json(f"{base}/collections/{dest_collection}")
    except Failure as exc:
        if "→ 404" not in str(exc):
            raise
    if existing is not None:
        if not force:
            raise Failure(f"目标 collection {dest_collection} 已存在；覆盖需 --force")
        http_json(f"{base}/collections/{dest_collection}", method="DELETE")
    proc = subprocess.run(
        [
            "curl",
            "-sS",
            "-F",
            f"snapshot=@{snapshot}",
            f"{base}/collections/{dest_collection}/snapshots/upload?priority=snapshot&wait=true",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise Failure(f"Qdrant 快照上传失败：{proc.stderr.strip()[:500]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Failure(f"Qdrant 上传返回非 JSON：{proc.stdout[:300]}") from exc
    if payload.get("status") not in (None, "ok"):
        raise Failure(f"Qdrant 上传失败：{payload}")
    info = http_json(f"{base}/collections/{dest_collection}")
    points = (info.get("result") or {}).get("points_count")
    expected = entry.get("points_count")
    if expected is not None and points != expected:
        raise Failure(f"Qdrant 点数不一致：恢复 {points}，备份 {expected}")
    return {"collection": dest_collection, "points_count": points}


def restore_data(target: Path, manifest: dict[str, Any], dest: Path, force: bool) -> dict[str, Any]:
    entry = manifest.get("data") or {}
    if not entry.get("dir"):
        raise Failure("manifest 里没有 data 备份")
    source = target / entry["dir"]
    if not source.is_dir():
        raise Failure(f"data 备份不存在：{source}")
    if dest.exists() and any(dest.iterdir()):
        if not force:
            raise Failure(f"目标目录非空：{dest}；覆盖需 --force")
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["rsync", "-a", f"{source}/", f"{dest}/"], check=False, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise Failure(f"rsync 恢复失败：{proc.stderr.strip()[:500]}")
    manifest_path = target / entry["sha256_manifest"]
    if not manifest_path.is_file():
        raise Failure(f"data 校验清单不存在：{manifest_path}")
    mismatches = 0
    checked = 0
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, rel = line.split("  ", 1)
        rel = rel.removeprefix("./")
        path = dest / rel
        checked += 1
        if not path.is_file() or sha256_file(path) != expected:
            mismatches += 1
            if mismatches <= 5:
                warn(f"校验失败：{rel}")
    if mismatches:
        raise Failure(f"data 校验失败：{mismatches}/{checked} 个文件不一致")
    return {"dir": str(dest), "files": checked}


def do_restore(args: argparse.Namespace) -> int:
    target = Path(args.target or os.environ.get("QS_BACKUP_DIR") or DEFAULT_TARGET).expanduser().resolve()
    manifest_path = target / "manifests" / f"{args.source}.json"
    if not manifest_path.is_file():
        warn(f"找不到备份清单：{manifest_path}")
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("partial"):
        warn("该备份标记 partial=true；缺项可能恢复失败")
    if not (args.pg_db or args.qdrant_collection or args.data_target):
        warn("至少指定 --pg-db / --qdrant-collection / --data-target 之一")
        return 2
    results: dict[str, Any] = {}
    failures: list[str] = []
    if args.pg_db:
        try:
            results["pg"] = restore_pg(target, manifest, args.pg_source, args.pg_db, args.force)
        except Failure as exc:
            failures.append(f"pg: {exc}")
    if args.qdrant_collection:
        try:
            results["qdrant"] = restore_qdrant(target, manifest, args.qdrant_collection, args.force)
        except Failure as exc:
            failures.append(f"qdrant: {exc}")
    if args.data_target:
        try:
            results["data"] = restore_data(target, manifest, Path(args.data_target).expanduser().resolve(), args.force)
        except Failure as exc:
            failures.append(f"data: {exc}")
    summary = {"source": args.source, "target": str(target), "results": results, "failures": failures}
    log(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if failures else 0


# ── CLI ───────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QuantSage 备份/恢复（SPEC §7.3）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    backup = sub.add_parser("backup", help="PG + Qdrant + data 备份")
    backup.add_argument("--target", help="备份落点（默认 ~/QuantSageBackups）")
    backup.add_argument("--keep-daily", type=int, default=7)
    backup.add_argument("--keep-weekly", type=int, default=4)
    backup.add_argument("--no-pg", action="store_true")
    backup.add_argument("--no-qdrant", action="store_true")
    backup.add_argument("--no-data", action="store_true")
    backup.add_argument("--dry-run", action="store_true")
    backup.set_defaults(func=do_backup)

    restore = sub.add_parser("restore", help="恢复到 scratch 目标")
    restore.add_argument("--source", required=True, help="备份日期 YYYYMMDD")
    restore.add_argument("--target", help="备份落点（默认 ~/QuantSageBackups）")
    restore.add_argument("--pg-db", help="恢复到的目标库（默认不恢复 PG）")
    restore.add_argument("--pg-source", default="quantsage", help="来源库名（默认 quantsage）")
    restore.add_argument("--qdrant-collection", help="恢复到的目标 collection")
    restore.add_argument("--data-target", help="恢复到的目标目录")
    restore.add_argument("--force", action="store_true", help="允许覆盖已存在的目标")
    restore.set_defaults(func=do_restore)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
