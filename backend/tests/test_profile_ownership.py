from app.v2.db import DB
from app.v3.identity import set_current_user, reset_current_user
from app.v3.schema import ensure_schema
from app.v3.service import V3Service
from types import SimpleNamespace


def test_profiles_are_isolated_and_legacy_row_stays_local(tmp_path):
    db = DB(str(tmp_path / "profiles.db"))
    db.init()
    db.update_profile(nickname="Legacy", fact_sheet={"private": "legacy"})
    token = set_current_user("user_alice")
    try:
        assert db.get_profile()["nickname"] != "Legacy"
        db.update_profile(nickname="Alice", fact_sheet={"private": "alice"})
    finally:
        reset_current_user(token)
    token = set_current_user("user_bob")
    try:
        assert db.get_profile()["fact_sheet"].get("private") is None
        db.update_profile(nickname="Bob")
    finally:
        reset_current_user(token)
    assert db.get_profile()["nickname"] == "Legacy"
    assert db.get_profile(user_id="user_alice")["fact_sheet"] == {"private": "alice"}
    assert db.get_profile(user_id="user_bob")["nickname"] == "Bob"
    db.close()


def test_profile_update_only_changes_the_owners_posts(tmp_path):
    db = DB(str(tmp_path / "profiles.db"))
    db.init()
    ensure_schema(db, seed=False)
    service = V3Service(db, SimpleNamespace(llm=None), str(tmp_path))
    alice = service.create_user_post("user_alice", "Alice post", author_name="Alice")
    bob = service.create_user_post("user_bob", "Bob post", author_name="Bob")
    service.update_profile_fields(user_id="user_alice", nickname="Alice Updated")
    assert service.get_user_post(alice["id"])["author_name"] == "Alice Updated"
    assert service.get_user_post(bob["id"])["author_name"] == "Bob"
    assert db.get_profile()["nickname"] != "Alice Updated"
    db.close()
