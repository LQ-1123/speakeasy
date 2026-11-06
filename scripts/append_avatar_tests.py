"""追加：种子头像接口测试。"""

import io

p = "backend/tests/test_v3_world.py"
s = io.open(p, encoding="utf-8").read()

anchor = "def test_health_reports_world(world_db):"
assert anchor in s

new_tests = '''def test_avatar_is_seeded_deterministic_svg(world_db):
    """头像由种子确定性生成：同 seed 同图、不同 seed 不同图、纯 SVG。"""
    db, tmp_path = world_db
    app, _service, _voice = _make_app(db, tmp_path)
    with TestClient(app) as client:
        a = client.get("/api/v3/avatar/persona-jiwoo.svg?g=female&r=east_asian")
        b = client.get("/api/v3/avatar/persona-jiwoo.svg?g=female&r=east_asian")
        c = client.get("/api/v3/avatar/persona-adrian.svg?g=male&r=white")
        assert a.status_code == 200 and c.status_code == 200
        assert a.headers["content-type"].startswith("image/svg+xml")
        assert "immutable" in a.headers.get("cache-control", "")
        assert a.text.startswith("<svg") and "</svg>" in a.text
        assert a.text == b.text, "同一 seed 必须生成同一张脸"
        assert a.text != c.text, "不同 seed 应该生成不同的脸"
        # 性别/地域参数会改变肤色/发型，从而改变结果
        d = client.get("/api/v3/avatar/persona-jiwoo.svg?g=male&r=black")
        assert d.text != a.text


def test_seed_data_has_no_mock_images():
    """Persona 头像不再依赖 mock 图片，全部走种子生成。"""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import importlib

    seed_data = importlib.import_module("app.v3.seed_data")
    assert seed_data.PERSONAS
    for persona in seed_data.PERSONAS:
        assert persona.get("avatar_seed"), persona["id"]
        assert "avatar" not in persona or not persona["avatar"]
        assert not str(persona.get("avatar", "")).startswith("/mock/")


def test_persona_avatar_url_encodes_seed_and_identity():
    from app.v3.avatar import persona_avatar_url

    url = persona_avatar_url(
        {"id": "p_jiwoo", "gender": "female", "country": "Korea"}
    )
    assert url.startswith("/api/v3/avatar/p_jiwoo.svg")
    assert "g=female" in url and "r=east_asian" in url


'''

s = s.replace(anchor, new_tests + anchor, 1)
io.open(p, "w", encoding="utf-8").write(s)
print("avatar tests appended")
