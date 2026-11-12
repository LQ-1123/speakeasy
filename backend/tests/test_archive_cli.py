"""The archive dry run must never initialize or modify its source database."""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path


def cli_module():
    path = Path(__file__).resolve().parents[2] / "scripts/archive_legacy_users.py"
    spec = importlib.util.spec_from_file_location("archive_legacy_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_readonly_prepare_then_apply_backed_up_manifest(tmp_path):
    cli = cli_module()
    db_path = tmp_path / "runtime" / "old.db"
    db_path.parent.mkdir()
    media = db_path.parent / "audio"
    media.mkdir()
    (media / "voice.wav").write_bytes(b"sample")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE v3_users(id TEXT PRIMARY KEY,created_at TEXT, token_hash TEXT)")
    conn.execute("INSERT INTO v3_users VALUES ('u_old','2026-11-01','token')")
    conn.commit()
    conn.close()
    before = db_path.read_bytes()
    manifest = cli.prepare_manifest(db_path)
    assert manifest["users"] == [{"id":"u_old", "created_at":"2026-11-01"}]
    assert db_path.read_bytes() == before
    backup_dir = tmp_path / "backups"
    result = cli.apply_manifest(db_path, manifest, backup_dir, db_path.parent)
    assert result["archived"] == 1
    backup = Path(result["backup"])
    copied = sqlite3.connect(backup / "database.sqlite3")
    assert copied.execute("SELECT token_hash FROM v3_users").fetchone()[0] == "token"
    copied.close()
    assert (backup / "media/audio/voice.wav").read_bytes() == b"sample"
    assert json.loads((backup / "manifest.json").read_text(encoding="utf-8"))["users"] == manifest["users"]


def test_backup_inside_media_source_is_rejected_before_any_write(tmp_path):
    import pytest
    cli = cli_module()
    source = tmp_path / "source.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE v3_users(id TEXT PRIMARY KEY,created_at TEXT)")
    conn.commit()
    conn.close()
    before = source.read_bytes()
    with pytest.raises(ValueError, match="备份"):
        cli.apply_manifest(source, cli.prepare_manifest(source), tmp_path / "backup", tmp_path)
    assert before == source.read_bytes()


def test_missing_media_root_never_archives(tmp_path):
    import pytest
    cli = cli_module()
    source = tmp_path / "source.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE v3_users(id TEXT PRIMARY KEY,created_at TEXT)")
    conn.execute("INSERT INTO v3_users VALUES ('u_old','2026-11-01')")
    conn.commit(); conn.close()
    before = source.read_bytes()
    with pytest.raises(ValueError, match="媒体"):
        cli.apply_manifest(source, cli.prepare_manifest(source), tmp_path / "backups", tmp_path / "misspelled")
    assert before == source.read_bytes()
