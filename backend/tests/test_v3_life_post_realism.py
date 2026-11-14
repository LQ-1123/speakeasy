"""Regression checks for source ownership, missing output and persona continuity."""

from types import SimpleNamespace

import pytest

from app.v2.db import DB
from app.v3 import life_posts as lp
from app.v3.schema import ensure_schema


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setenv("SE_MEDIA_ENABLED", "1")
    db = DB(str(tmp_path / "life_posts.db"))
    db.init()
    ensure_schema(db, seed=False)
    with db._lock, db.conn:
        db.conn.execute(
            "INSERT INTO personas (id,nickname,language,speech_dna) VALUES (?,?,?,?)",
            ("p_writer", "Camille", "fr", '{"verbosity":"short"}'),
        )
    yield db, {"id": "p_writer", "nickname": "Camille", "language": "fr"}, tmp_path
    db.close()


async def test_no_model_output_does_not_create_canned_weather_or_motto(scene, monkeypatch):
    db, persona, tmp_path = scene

    async def no_image(*args):
        return None

    monkeypatch.setattr(lp, "_pick_image", no_image)
    result = await lp.generate_life_posts(
        db, None, [persona], media_dir=str(tmp_path), expires_at="",
    )
    assert result == {"image_posts": 0, "text_posts": 0, "skipped_images": 1}
    assert db.conn.execute("SELECT count(*) FROM persona_posts").fetchone()[0] == 0


async def test_life_text_uses_native_language_dna_and_recent_posts(scene, monkeypatch):
    db, persona, tmp_path = scene
    lp._insert_post(
        db, persona_id=persona["id"], content="Je garde ce livre.",
        language="fr", expires_at="",
    )
    captured = []

    async def no_image(*args):
        return None

    async def complete(llm, messages, **kwargs):
        captured.append(messages[0]["content"])
        return {"content": "Je garde ce livre."}

    monkeypatch.setattr(lp, "_pick_image", no_image)
    monkeypatch.setattr(lp, "_llm_json", complete)
    result = await lp.generate_life_posts(
        db, SimpleNamespace(), [persona], media_dir=str(tmp_path), expires_at="",
    )
    assert result["text_posts"] == 0  # Do not publish the same thought again.
    assert "Write in fr" in captured[0]
    assert "verbosity" in captured[0] and "Je garde ce livre." in captured[0]
    assert db.conn.execute("SELECT count(*) FROM persona_posts").fetchone()[0] == 1


async def test_stock_photo_cannot_be_published_as_own_experience(scene, monkeypatch):
    db, persona, tmp_path = scene
    from app.v3.media import MediaItem

    item = MediaItem(
        external_id="test:coffee", source="Wikimedia", title="Coffee cup",
        tags=["coffee"], license="CC0", author="Someone else", page_url="https://example.org/photo",
    )
    captured = []

    async def pick(*args):
        return item

    async def download(*args):
        return "/media/coffee.jpg"

    async def complete(llm, messages, **kwargs):
        captured.append(messages[0]["content"])
        return {"content": "I took this photo in my kitchen."} if len(captured) == 1 else {}

    monkeypatch.setattr(lp, "_pick_image", pick)
    monkeypatch.setattr(lp, "download_image", download)
    monkeypatch.setattr(lp, "_llm_json", complete)
    result = await lp.generate_life_posts(
        db, SimpleNamespace(), [persona], media_dir=str(tmp_path), expires_at="",
    )
    assert result["image_posts"] == result["text_posts"] == 0
    assert "SOMEONE ELSE'S" in captured[0]
    assert "NOT visually inspected" in captured[0]
    assert db.conn.execute("SELECT count(*) FROM persona_posts").fetchone()[0] == 0
