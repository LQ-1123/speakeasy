"""给压测准备一个**独立的**数据目录（绝不碰线上 data/）。

生成内容
    - N 个已验证邮箱账号 + 各自一个会话 token（生产同款 X-Session-Token 鉴权路径）
    - 每人 K 个英语人物好友（走 V3Service.add_friend，Persona 生活层/音色绑定都是真的）
    - 每个好友 H 条历史消息（好友消息带语音文件名并登记媒体归属，覆盖签名 URL 路径）
    - tts_config.json 指向假 Fish；users.json 给 loadgen.py 用

    PYTHONPATH=backend python scripts/perf/seed_bench_data.py --data-dir .tmp/perf/data --users 150
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from perf_utils import require_loopback, isolated_env, install_loopback_guard
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))

USER_LINES = (
    "I finally tried that noodle place near my office today.",
    "Work was a bit much, I had three meetings back to back.",
    "Do you think it is weird to study at night instead of the morning?",
    "I watched half a movie and then fell asleep, to be honest.",
)
FRIEND_LINES = (
    "Oh nice, was it as good as people say? I always end up ordering the same thing.",
    "Three in a row sounds exhausting. Did you at least get a proper lunch?",
    "Not weird at all. I do my best thinking after ten, honestly.",
    "Ha, that is the best kind of evening. Which movie was it?",
)


def seed_provider_pool(db, data_dir, fake_upstream, *, fish_accounts=0,
                       fish_concurrency=5, llm_accounts=0, llm_concurrency=10):
    """Activate explicit fake fixtures locally; never call a verification provider.

    Credentials are generated here, never inherited. Every endpoint is checked
    before creating the encrypted store, and every account owns an independent
    quota group. This fixture is not evidence of real provider verification.
    """
    fake = require_loopback(fake_upstream)
    if min(fish_accounts, llm_accounts) < 0 or min(fish_concurrency, llm_concurrency) < 1:
        raise ValueError("Synthetic account counts/capacities must be valid")
    result = {"synthetic_verification": True, "external_provider_budget": 0, "providers": {}}
    if not fish_accounts and not llm_accounts:
        return result
    from app.providers.store import ProviderStore
    store = ProviderStore(db, data_dir)
    for provider, count, concurrency, model, endpoint in (
        ("fish", fish_accounts, fish_concurrency, "s2.1-pro-free", fake),
        ("llm", llm_accounts, llm_concurrency, "fake-chat", fake + "/v1"),
    ):
        for index in range(count):
            item = store.create({"provider": provider, "name": f"synthetic-{provider}-{index + 1:04d}",
                "api_key": f"perf-fake-{provider}-account-{index + 1:04d}",
                "base_url": endpoint, "model": model, "concurrency_limit": concurrency})
            store.verification_result(item["id"], item["revision"], True)
        if count:
            result["providers"][provider] = {"accounts": count, "per_account_concurrency": concurrency,
                "independent_total_concurrency": count * concurrency, "model": model}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--users", type=int, default=150)
    parser.add_argument("--friends", type=int, default=5, help="每人好友数（≤ 英语种子人物数）")
    parser.add_argument("--history", type=int, default=200, help="每个好友的历史消息条数")
    parser.add_argument("--fake-upstream", default="http://127.0.0.1:9100")
    parser.add_argument("--pool-fish-accounts", type=int, default=0)
    parser.add_argument("--pool-fish-concurrency", type=int, default=5)
    parser.add_argument("--pool-llm-accounts", type=int, default=0)
    parser.add_argument("--pool-llm-concurrency", type=int, default=10)
    parser.add_argument("--unread-backlog", type=int, default=0,
                        help="预置 N 条「未读消息通知」（模拟一批用户很久没打开 App），用来压事件收集循环")
    args = parser.parse_args()
    args.fake_upstream = require_loopback(args.fake_upstream)
    install_loopback_guard()

    data_dir = os.path.abspath(args.data_dir)
    if data_dir in {os.path.join(ROOT, "data"), os.path.join(ROOT, "backend", "data")}:
        parser.error("Refusing to seed the application's real data directory")
    clean = isolated_env(data_dir, args.fake_upstream)
    os.environ.clear()
    os.environ.update(clean)
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "speakeasy_v2.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)

    os.environ["SE_LOAD_DOTENV"] = "0"
    os.environ["DATA_DIR"] = data_dir
    os.environ["SE_EMAIL_CODE_AUTH"] = "1"

    from app import config
    config.Settings.model_config["env_file"] = None
    config.get_settings.cache_clear()

    from app.v2.db import DB
    from app.v3 import accounts
    from app.v3.ads import ensure_ads_schema
    from app.v3.client_api import ensure_client_schema
    from app.v3.media_access import ensure_media_schema
    from app.v3.privacy import ensure_privacy_schema
    from app.v3.recommend import ensure_recommend_schema
    from app.v3.reports import ensure_reports_schema
    from app.v3.schema import ensure_schema
    from app.v3.service import V3Service
    from app.v3.teen import ensure_teen_schema

    db = DB(db_path)
    db.init()
    ensure_schema(db, seed=True)
    for ensure in (ensure_client_schema, accounts.ensure_accounts_schema, ensure_privacy_schema,
                   ensure_media_schema, ensure_teen_schema, ensure_recommend_schema,
                   ensure_ads_schema, ensure_reports_schema):
        ensure(db)
    conn = db.conn
    conn.execute("PRAGMA synchronous=OFF")  # 只为灌数据快；服务启动时会按自己的配置重设

    with db._lock, conn:  # noqa: SLF001
        # 每个人物一个专属 Fish 音色：与线上「一人一色」一致，TTS 才会真的走 Fish 链路
        for row in conn.execute("SELECT id FROM personas").fetchall():
            conn.execute("UPDATE personas SET fish_voice_id=?, status='active' WHERE id=?",
                         (f"fishvoice-{row['id']}", row["id"]))
    english = [r["id"] for r in conn.execute(
        "SELECT id FROM personas WHERE language='en' AND status='active' ORDER BY id")]
    if not english:
        raise SystemExit("种子里没有英语人物，无法准备好友")
    service = V3Service(db, None, data_dir)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    users = []
    started = time.time()
    for index in range(args.users):
        uid = f"u_perf{index:05d}"
        accounts._ensure_user_row(db, uid, f"Perf {index}")  # noqa: SLF001
        accounts._insert_account(db, uid, f"perf{index}@example.com", "perf-not-a-real-hash")  # noqa: SLF001
        with db._lock, conn:  # noqa: SLF001
            conn.execute("UPDATE v3_accounts SET email_verified_at=? WHERE user_id=?", (stamp, uid))
        session = accounts.create_session(db, uid, platform="perf")
        db.update_profile(
            user_id=uid, nickname=f"Perf{index}", native_language="zh-CN", target_languages=["en"],
            profile_setup_completed_at=stamp,
            fact_sheet={"profile": {"nickname": f"Perf{index}", "city": "Shanghai"},
                        "interests": ["coffee", "movies"],
                        "language_profile": {"target": "en", "levels": {"en": "intermediate"}}},
        )
        friend_ids = []
        for persona_id in english[: max(1, args.friends)]:
            friend_id = service.add_friend(uid, persona_id)["friend_id"]
            friend_ids.append(friend_id)
            rows, assets = [], []
            for n in range(args.history):
                created = (now - timedelta(minutes=(args.history - n) * 7)).isoformat(timespec="seconds")
                if n % 2 == 0:
                    rows.append((uuid.uuid4().hex, friend_id, "user", "text",
                                 USER_LINES[(n // 2) % len(USER_LINES)], None, 0, 1, created))
                else:
                    audio = f"fish_{uuid.uuid4().hex[:12]}.mp3"
                    rows.append((uuid.uuid4().hex, friend_id, "friend", "voice",
                                 FRIEND_LINES[(n // 2) % len(FRIEND_LINES)], audio, 4200, 1, created))
                    assets.append(("ma_" + uuid.uuid4().hex, uid, "audio", audio, created))
            with db._lock, conn:  # noqa: SLF001
                conn.executemany(
                    "INSERT INTO messages (id, friend_id, sender, msg_type, transcript, audio_path,"
                    " duration_ms, is_read, created_at) VALUES (?,?,?,?,?,?,?,?,?)", rows)
                conn.executemany(
                    "INSERT INTO media_assets (asset_id, owner_user_id, kind, filename, created_at)"
                    " VALUES (?,?,?,?,?)", assets)
        # 老好友都已经「介绍过新朋友」：否则每个好友的第一轮都会触发一次性的好友裂变
        # （多一次 LLM 调用 + 新建好友），把稳态成本测高。
        with db._lock, conn:  # noqa: SLF001
            for position, friend_id in enumerate(friend_ids):
                referrer = friend_ids[(position + 1) % len(friend_ids)]
                if referrer != friend_id:
                    conn.execute("UPDATE friends SET referred_by_friend_id=? WHERE id=?", (referrer, friend_id))
        users.append({"user_id": uid, "token": session["session_token"], "friends": friend_ids})

    if args.unread_backlog > 0:
        with db._lock, conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT m.id, m.friend_id, m.created_at, f.owner_user_id FROM friends f JOIN messages m"
                " ON m.rowid=(SELECT x.rowid FROM messages x WHERE x.friend_id=f.id AND x.sender='friend'"
                " ORDER BY x.created_at DESC, x.rowid DESC LIMIT 1) LIMIT ?", (args.unread_backlog,)).fetchall()
            for row in rows:
                conn.execute("UPDATE messages SET is_read=0 WHERE id=?", (row["id"],))
                conn.execute(
                    "INSERT OR IGNORE INTO client_events (id,user_id,type,friend_id,payload,created_at,next_attempt)"
                    " VALUES (?,?,'message',?,?,?,0)",
                    ("message:" + row["id"], row["owner_user_id"], row["friend_id"],
                     json.dumps({"name": "friend", "text": "hello", "route": "#/chat/" + row["friend_id"]}),
                     row["created_at"]))
            latest = conn.execute("SELECT COALESCE(MAX(rowid),0) FROM messages").fetchone()[0]
            conn.execute("INSERT OR REPLACE INTO client_event_state VALUES ('message_row',?)", (str(latest),))
            conn.execute("INSERT OR REPLACE INTO client_event_state VALUES ('installed_at',?)", (stamp,))

    counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
              for name in ("v3_accounts", "friends", "messages", "media_assets", "personas", "persona_posts",
                           "client_events")}
    fixture = seed_provider_pool(db, data_dir, args.fake_upstream,
        fish_accounts=args.pool_fish_accounts, fish_concurrency=args.pool_fish_concurrency,
        llm_accounts=args.pool_llm_accounts, llm_concurrency=args.pool_llm_concurrency)
    db.close()

    with open(os.path.join(data_dir, "tts_config.json"), "w", encoding="utf-8") as fh:
        json.dump({"provider": "fish", "fish_api_key": "perf-fake-key",
                   "fish_base_url": args.fake_upstream, "fish_model": "s2.1-pro-free",
                   "fish_format": "wav"}, fh)
    with open(os.path.join(data_dir, "users.json"), "w", encoding="utf-8") as fh:
        json.dump(users, fh)
    with open(os.path.join(data_dir, "provider-fixture.json"), "w", encoding="utf-8") as fh:
        json.dump(fixture, fh)
    print(f"seeded {data_dir} in {time.time() - started:.1f}s: {counts}")


if __name__ == "__main__":
    main()
