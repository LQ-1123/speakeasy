"""Prepare a read-only identity manifest; archive only after a consistent backup.

Usage: --database FILE --prepare manifest.json
       --database FILE --manifest manifest.json --apply --maintenance-confirmed
       --backup-dir DIR --media-root DIR

Stop application/workers before --apply. Never invoke cleanup_empty_users.py for
this cutover. Backups contain private data and must stay outside the served tree.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.v3.account_archive import archive_users


def _readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def prepare_manifest(database: Path) -> dict:
    with closing(_readonly(database)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(v3_users)")}
        where = " WHERE archived_at=''" if "archived_at" in cols else ""
        users = [dict(r) for r in conn.execute("SELECT id,created_at FROM v3_users" + where + " ORDER BY id")]
    return {"version": 1, "database": str(database.resolve()),
            "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "users": users}


def apply_manifest(database: Path, manifest: dict, backup_dir: Path, media_root: Path) -> dict:
    database, backup_dir, media_root = database.resolve(), backup_dir.resolve(), media_root.resolve()
    if backup_dir == media_root or media_root in backup_dir.parents:
        raise ValueError("备份目录必须位于媒体源目录之外")
    if not media_root.is_dir():
        raise ValueError("媒体源目录必须存在且为目录")
    if manifest.get("version") != 1 or manifest.get("database") != str(database):
        raise ValueError("清单版本或数据库路径不一致")
    users = manifest.get("users")
    if not isinstance(users, list) or not users:
        raise ValueError("清单没有待归档身份")
    expected = {str(row["id"]): str(row["created_at"] or "") for row in users}
    if len(expected) != len(users) or not all(expected):
        raise ValueError("清单包含重复或空身份")
    with closing(_readonly(database)) as reader:
        for uid, created in expected.items():
            row = reader.execute("SELECT created_at FROM v3_users WHERE id=?", (uid,)).fetchone()
            if row is None or str(row[0] or "") != created:
                raise ValueError("身份与清单不一致，请重新演练")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
        target = backup_dir / ("email-auth-" + stamp)
        target.mkdir(parents=True)
        with sqlite3.connect(target / "database.sqlite3") as output:
            reader.backup(output)
    # Exclude DB/WAL/SHM: sqlite backup above is the coherent database snapshot.
    ignored = {database.name, database.name + "-wal", database.name + "-shm"}
    shutil.copytree(media_root, target / "media", ignore=lambda directory, names:
                    ignored if Path(directory).resolve() == database.parent else set())
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        db = SimpleNamespace(conn=conn, _lock=threading.RLock())
        result = archive_users(db, list(expected), migration_id=stamp, expected_created_at=expected)
    finally:
        conn.close()
    return {**result, "backup": str(target)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--prepare", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--maintenance-confirmed", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--media-root", type=Path)
    args = parser.parse_args()
    if args.apply:
        if not all((args.maintenance_confirmed, args.manifest, args.backup_dir, args.media_root)):
            parser.error("apply 需要 --maintenance-confirmed、--manifest、--backup-dir 和 --media-root")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        result = apply_manifest(args.database, manifest, args.backup_dir, args.media_root)
    else:
        result = prepare_manifest(args.database)
        if args.prepare:
            # Exclusive creation prevents overwriting a reviewed manifest.
            with args.prepare.open("x", encoding="utf-8") as output:
                json.dump(result, output, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
