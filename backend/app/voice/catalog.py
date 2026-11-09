"""Transactional repair and reservation of persona voice identities."""

from __future__ import annotations

from typing import Any
import asyncio
import os
import time

from app.v3.seed_data import LANGUAGE_VOICES


def edge_voice_candidates(language: str, gender: str = "") -> tuple[str, ...]:
    voices = LANGUAGE_VOICES.get(str(language).strip().lower(), ())
    female_names = ("Ava", "Sonia", "Jenny", "Nanami", "SunHi", "Ximena", "Isabella",
                    "Thalita", "Vivienne", "Xiaoyi", "Seraphina")
    if gender == "female":
        return tuple(v for v in voices if any(name in v for name in female_names))
    if gender == "male":
        return tuple(v for v in voices if not any(name in v for name in female_names))
    return voices


def _ensure_catalog(db: Any) -> None:
    db.conn.execute(
        "CREATE TABLE IF NOT EXISTS voice_catalog (provider TEXT NOT NULL,reference_id TEXT NOT NULL,"
        "verified_at REAL NOT NULL DEFAULT 0,last_error TEXT NOT NULL DEFAULT '',"
        "PRIMARY KEY(provider,reference_id))"
    )


def verified_voice(db: Any, provider: str, reference_id: str) -> bool:
    with db._lock, db.conn:
        _ensure_catalog(db)
        row = db.conn.execute(
            "SELECT verified_at FROM voice_catalog WHERE provider=? AND reference_id=?",
            (provider, reference_id),
        ).fetchone()
    return bool(row and float(row[0]) > 0)


async def verify_voice(
    db: Any, provider: str, reference_id: str, *, language: str = "en", cfg: Any = None,
    edge_engine: Any = None,
) -> bool:
    """Readiness requires a successful synthesis, never provider-list metadata."""
    if not reference_id:
        return False
    if verified_voice(db, provider, reference_id):
        return True
    error = ""
    try:
        if provider == "fish":
            from app.voice import fish_tts, voice_registry

            cfg = cfg if cfg is not None else voice_registry.load_cfg()
            audio = await fish_tts.synthesize(
                "Hello, nice to meet you.",
                api_key=str(getattr(cfg, "fish_api_key", "") or ""),
                base_url=str(getattr(cfg, "fish_base_url", "") or ""),
                model=str(getattr(cfg, "fish_model", "") or ""),
                reference_id=reference_id, audio_format="mp3", timeout=8.0,
            )
            if len(audio or b"") < 256:
                raise ValueError("empty_voice_sample")
        elif provider == "edge":
            if edge_engine is None:
                from app.voice.edge_tts_engine import EdgeTTSEngine

                edge_engine = EdgeTTSEngine()
            audio = await asyncio.wait_for(edge_engine.synthesize_voice(
                "Hello, nice to meet you.", reference_id, rate="+0%", pitch="+0Hz"
            ), timeout=8.0)
            if len(audio) < 2400:
                raise ValueError("empty_voice_sample")
        else:
            raise ValueError("unsupported_voice_provider")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
    with db._lock, db.conn:
        _ensure_catalog(db)
        db.conn.execute(
            "INSERT INTO voice_catalog(provider,reference_id,verified_at,last_error) VALUES (?,?,?,?) "
            "ON CONFLICT(provider,reference_id) DO UPDATE SET verified_at=excluded.verified_at,last_error=excluded.last_error",
            (provider, reference_id, 0 if error else time.time(), error),
        )
    return not error


def free_edge_voice(db: Any, language: str, *, exclude_persona: str = "", gender: str = "") -> str:
    """Caller holds db._lock and its write transaction while reserving the result."""
    used = {str(row[0]) for row in db.conn.execute(
        "SELECT voice_id FROM personas WHERE status='active' AND id<>?", (exclude_persona,)
    )}
    return next((v for v in edge_voice_candidates(language, gender) if v not in used), "")


def used_voice_ids(db: Any, column: str) -> set[str]:
    if column not in ("voice_id", "fish_voice_id"):
        raise ValueError("unsupported_voice_column")
    voices: set[str] = set()
    with db._lock:
        for table in ("personas", "friends"):
            if not db.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                continue
            voices.update(str(r[0]) for r in db.conn.execute(
                f"SELECT {column} FROM {table} WHERE status IN ('active','pending_request') AND COALESCE({column},'')<>''"
            ))
    return voices


def repair_persona_voices(db: Any) -> int:
    """Repair only collisions/invalid bindings, retaining established first owners."""
    changed = 0
    with db._lock, db.conn:
        if not db.conn.in_transaction:
            db.conn.execute("BEGIN IMMEDIATE")
        rows = [dict(r) for r in db.conn.execute(
            "SELECT * FROM personas ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END,created_at,id"
        )]
        used_edge: set[str] = set()
        used_fish: set[str] = set()
        reserved_edge = {str(r.get("voice_id") or "") for r in rows if r["status"] == "active"}
        strict = os.getenv("SE_VOICE_STRICT", "0").lower() in ("1", "true", "yes")
        edge_main = {str(r.get("voice_id") or "") for r in rows
                     if r["status"] == "active" and not str(r.get("fish_voice_id") or "")}
        for row in rows:
            if row["status"] not in ("active", "waiting_voice"):
                continue
            original_edge = str(row.get("voice_id") or "")
            edge = original_edge
            fish = str(row.get("fish_voice_id") or "")
            lang = str(row.get("language") or "en")
            catalog = edge_voice_candidates(lang, str(row.get("gender") or ""))
            if fish in used_fish:
                fish = ""
            preserved_fish = strict and bool(fish) and row["status"] == "active"
            if preserved_fish and (edge in used_edge or edge in edge_main):
                edge = ""
            elif not preserved_fish and (not edge or edge in used_edge):
                edge = next((v for v in catalog if v not in used_edge and v not in reserved_edge), "")
            status = "active" if edge or fish else "waiting_voice"
            if strict and not preserved_fish and (row["status"] == "waiting_voice" or edge != original_edge):
                provider, ref = ("fish", fish) if fish else ("edge", edge)
                _ensure_catalog(db)
                check = db.conn.execute(
                    "SELECT verified_at FROM voice_catalog WHERE provider=? AND reference_id=?", (provider, ref)
                ).fetchone()
                if not check or not float(check[0]):
                    status = "waiting_voice"
            if (edge, fish, status) != (original_edge, str(row.get("fish_voice_id") or ""), row["status"]):
                db.conn.execute(
                    "UPDATE personas SET voice_id=?,fish_voice_id=?,status=? WHERE id=?",
                    (edge, fish, status, row["id"]),
                )
                changed += 1
            if edge and status == "active":
                used_edge.add(edge)
            if fish and status == "active":
                used_fish.add(fish)
        # Unique indexes enforce the invariant across independent workers/processes.
        db.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_persona_edge "
            "ON personas(voice_id) WHERE status='active' AND voice_id<>''"
        )
        db.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_persona_fish "
            "ON personas(fish_voice_id) WHERE status='active' AND fish_voice_id<>''"
        )
        db.conn.execute(
            "UPDATE friends SET voice_id=(SELECT p.voice_id FROM personas p WHERE p.id=friends.persona_id),"
            "fish_voice_id=(SELECT p.fish_voice_id FROM personas p WHERE p.id=friends.persona_id) "
            "WHERE EXISTS(SELECT 1 FROM personas p WHERE p.id=friends.persona_id)"
        )
    return changed


def voice_gaps(db: Any) -> dict[str, Any]:
    with db._lock:
        rows = db.conn.execute(
            "SELECT language,gender,COUNT(*) AS n FROM personas WHERE status='waiting_voice' GROUP BY language,gender"
        ).fetchall()
    groups = [{"language": r[0], "gender": r[1], "count": int(r[2])} for r in rows]
    return {"waiting_voice": sum(r["count"] for r in groups), "gaps": groups}


def _readiness_batch(db: Any, table: str, limit: int) -> list[dict]:
    """Persist cooldown before provider I/O, so failures and restarts cannot starve later roles."""
    if table not in ("personas", "friends"):
        raise ValueError("unsupported_readiness_table")
    now = time.time()
    with db._lock, db.conn:
        if not db.conn.in_transaction:
            db.conn.execute("BEGIN IMMEDIATE")
        db.conn.execute(
            "CREATE TABLE IF NOT EXISTS voice_readiness_attempts ("
            "entity_kind TEXT NOT NULL,entity_id TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,"
            "last_attempt REAL NOT NULL DEFAULT 0,retry_after REAL NOT NULL DEFAULT 0,"
            "PRIMARY KEY(entity_kind,entity_id))"
        )
        standalone = " AND COALESCE(r.persona_id,'')=''" if table == "friends" else ""
        rows = db.conn.execute(
            f"SELECT r.*,COALESCE(a.attempts,0) AS readiness_attempts FROM {table} r "
            "LEFT JOIN voice_readiness_attempts a ON a.entity_kind=? AND a.entity_id=r.id "
            "WHERE r.status='waiting_voice' AND COALESCE(a.retry_after,0)<=?" + standalone +
            " ORDER BY COALESCE(a.last_attempt,0),r.created_at,r.id LIMIT ?",
            (table, now, max(1, int(limit))),
        ).fetchall()
        pending = [dict(row) for row in rows]
        for row in pending:
            attempts = int(row["readiness_attempts"]) + 1
            retry_after = now + min(3600, 30 * (2 ** min(attempts - 1, 7)))
            db.conn.execute(
                "INSERT INTO voice_readiness_attempts(entity_kind,entity_id,attempts,last_attempt,retry_after) "
                "VALUES (?,?,?,?,?) ON CONFLICT(entity_kind,entity_id) DO UPDATE SET "
                "attempts=excluded.attempts,last_attempt=excluded.last_attempt,retry_after=excluded.retry_after",
                (table, row["id"], attempts, now, retry_after),
            )
    return pending


async def ensure_ready_personas(db: Any, *, limit: int = 10) -> int:
    """A low-priority job can recover waiting roles after synthesis becomes available."""
    pending = _readiness_batch(db, "personas", limit)
    activated = 0
    for row in pending:
        with db._lock:
            used = used_voice_ids(db, "voice_id")
            edge = str(row.get("voice_id") or "")
            if not edge or edge in used:
                edge = free_edge_voice(db, str(row.get("language") or "en"), exclude_persona=row["id"],
                                       gender=str(row.get("gender") or ""))
            fish = str(row.get("fish_voice_id") or "")
            if fish in used_voice_ids(db, "fish_voice_id"):
                fish = ""
        provider, reference = ("fish", fish) if fish else ("edge", edge)
        if not reference or not await verify_voice(db, provider, reference, language=str(row.get("language") or "en")):
            continue
        with db._lock, db.conn:
            if not db.conn.in_transaction:
                db.conn.execute("BEGIN IMMEDIATE")
            current = db.conn.execute("SELECT status FROM personas WHERE id=?", (row["id"],)).fetchone()
            if not current or current[0] != "waiting_voice":
                continue
            if reference in used_voice_ids(db, "fish_voice_id" if provider == "fish" else "voice_id"):
                continue
            db.conn.execute(
                "UPDATE personas SET status='active',voice_id=?,fish_voice_id=? WHERE id=?",
                (edge, fish, row["id"]),
            )
            db.conn.execute(
                "UPDATE persona_posts SET visibility='public' WHERE persona_id=? AND visibility='waiting_voice'",
                (row["id"],),
            )
            db.conn.execute(
                "UPDATE friends SET voice_id=?,fish_voice_id=? WHERE persona_id=?",
                (edge, fish, row["id"]),
            )
            db.conn.execute("DELETE FROM voice_readiness_attempts WHERE entity_kind='personas' AND entity_id=?", (row["id"],))
            activated += 1
    return activated


async def ensure_ready_friends(db: Any, *, limit: int = 10) -> int:
    """Recover standalone V2 friends without turning pending requests into friends."""
    pending = _readiness_batch(db, "friends", limit)
    activated = 0
    for row in pending:
        language = str(row.get("reply_language") or row.get("native_lang") or "en")
        with db._lock:
            edge = str(row.get("voice_id") or "")
            used = used_voice_ids(db, "voice_id")
            if not edge or edge in used:
                edge = next((v for v in edge_voice_candidates(language, str(row.get("gender") or "")) if v not in used), "")
            fish = str(row.get("fish_voice_id") or "")
            if fish in used_voice_ids(db, "fish_voice_id"):
                fish = ""
        provider, reference = ("fish", fish) if fish else ("edge", edge)
        if not reference or not await verify_voice(db, provider, reference, language=language):
            continue
        with db._lock, db.conn:
            if not db.conn.in_transaction:
                db.conn.execute("BEGIN IMMEDIATE")
            current = db.conn.execute("SELECT status FROM friends WHERE id=?", (row["id"],)).fetchone()
            if not current or current[0] != "waiting_voice":
                continue
            if reference in used_voice_ids(db, "fish_voice_id" if provider == "fish" else "voice_id"):
                continue
            status = str(row.get("voice_pending_status") or "pending_request")
            if status not in ("active", "pending_request"):
                status = "pending_request"
            db.conn.execute(
                "UPDATE friends SET status=?,voice_id=?,fish_voice_id=? WHERE id=?",
                (status, edge, fish, row["id"]),
            )
            db.conn.execute("DELETE FROM voice_readiness_attempts WHERE entity_kind='friends' AND entity_id=?", (row["id"],))
            activated += 1
    return activated
