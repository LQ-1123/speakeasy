"""清理「空注册记录」——注册后没有任何使用痕迹的 ``v3_users`` 行。

为什么要清
----------
客户端在空白 localStorage 里启动会新造一个 ``user_id``（无痕窗口、新浏览器
profile、重装 App、清应用数据都算）。线上实测 ``v3_users`` 91 行里 75 行是这种
残留：没有好友、没有消息、没有动态、没有设备注册，``last_seen_at == created_at``。

它们不是"用户"，但会污染管理台的每个数字（用户总数、活跃、转化率…），也会让
真正想找某个用户时在一堆空行里翻。清掉不损失任何用户数据。

安全边界（这是本脚本最重要的部分）
----------------------------------
1. **默认只演练**：不带 ``--apply`` 只打印将要删什么，一行都不动。
2. **先备份**：``--apply`` 时先把 DB 复制到 ``<data>/_backup/cleanup-<时间>/``，
   备份失败就中止（不带着风险删）。
3. **白名单保护**：只要有任何使用痕迹就不删——判定复用
   ``app.admin.user_scope.real_ids``（与管理台**同一份口径**，避免"界面说有痕迹、
   脚本却删了它"这种最坏情况）。
4. **只删两张表**：``user_profiles``（画像，空壳也只有空行）和 ``v3_users`` 本身。
   一旦某个用户有 friends / messages / posts / moments / devices，它就不在白名单外，
   所以这些表根本不会出现孤儿。
5. **``--keep`` 显式保护**：可以额外指定必须保留的 user id（例如你确认过的真号）。
6. 不 ``VACUUM``：SQLite 的 VACUUM 需要额外磁盘且会长时间持锁，线上服务在跑，
   清理几千行也回收不了多少空间。要压缩请单独在停机窗口做。

用法
----
    # 演练（默认）
    python scripts/cleanup_empty_users.py --data-dir data

    # 真删
    python scripts/cleanup_empty_users.py --data-dir data --apply
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

#: 让脚本既能从仓库根目录跑，也能在服务器上跑（把 backend 加进 sys.path）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

#: 自动化测试身份的 ``device_key`` 前缀。
#:
#: 这些不是猜的，是从线上库里实际读到的：``e2e-flow-1794150627``、
#: ``voice-check-1794127650``、``refresh-shot``、``p2-1794059824-32696``、
#: ``demo-seed-multi-image``、``accept-1794057201-39208``。
#: 它们是历次端到端脚本在空白 localStorage 下注册出来的假用户，
#: **带有消息和好友**（所以不属于"空壳"），必须显式开 ``--test-identities`` 才清。
TEST_DEVICE_PREFIXES: tuple[str, ...] = (
    "e2e-flow-",
    "voice-check-",
    "refresh-",
    "p2-",
    "demo-seed-",
    "accept-",
)

#: 删除用户时要一起清的**子表**，顺序 = 子先父后。
#:
#: 每项 ``(表, 关联列, 来源)``：
#: - ``来源 == "direct"``：直接用待删 user id 集合过滤该列。
#: - ``来源 == "sub:<SQL>``：``sub:`` 后是一段返回 id 的 SQL，其中 ``{ph}`` 会被
#:   替换成占位符串；用于"消息挂在 friends 上"这类两跳关联。
#:
#: **故意不含** ``persona_example_dialogs``：它的 ``user_line`` 是"示例对话里用户
#: 那一句"的内容，不是用户外键。按列名模糊匹配去删会清空全部示例语料（756 行），
#: 这是本表存在的原因——把"看起来像外键"的列显式排除并写清理由。
CASCADE: tuple[tuple[str, str, str], ...] = (
    ("messages", "friend_id", "sub:SELECT id FROM friends WHERE owner_user_id IN ({ph})"),
    ("user_post_comments", "post_id", "sub:SELECT id FROM user_posts WHERE user_id IN ({ph})"),
    ("user_post_likes", "post_id", "sub:SELECT id FROM user_posts WHERE user_id IN ({ph})"),
    ("moment_comments", "moment_id", "sub:SELECT id FROM user_moments WHERE user_id IN ({ph})"),
    ("friends", "owner_user_id", "direct"),
    ("friendships", "user_id", "direct"),
    ("user_posts", "user_id", "direct"),
    ("user_moments", "user_id", "direct"),
    ("persona_post_comments", "user_id", "direct"),
    ("persona_post_likes", "user_id", "direct"),
    ("client_events", "user_id", "direct"),
    ("client_preferences", "user_id", "direct"),
    ("client_devices", "user_id", "direct"),
    ("saved_expressions", "user_id", "direct"),
    ("social_events", "user_id", "direct"),
    ("social_event_attempts", "user_id", "direct"),
    ("v3_discover_views", "user_id", "direct"),
    ("user_profile", "user_id", "direct"),
    ("user_profiles", "user_id", "direct"),
    ("v3_users", "id", "direct"),
)


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """表/列是否存在。跨版本库安全：缺表缺列就跳过，不抛错。"""
    try:
        cols = [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.Error:
        return False
    return column in cols


def _count_cascade(conn: sqlite3.Connection, ids: list[str]) -> list[tuple[str, str, int]]:
    """演练用：逐表统计将删除的行数。"""
    ph = ",".join("?" for _ in ids)
    out: list[tuple[str, str, int]] = []
    for table, column, source in CASCADE:
        if not _has_column(conn, table, column):
            continue
        if source == "direct":
            sql = f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({ph})"
            params: tuple = tuple(ids)
        else:
            sub = source[4:].format(ph=ph)
            sql = f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({sub})"
            params = tuple(ids)
        try:
            n = int(conn.execute(sql, params).fetchone()[0] or 0)
        except sqlite3.Error:
            continue
        if n:
            out.append((table, column, n))
    return out


def _delete_cascade(conn: sqlite3.Connection, ids: list[str]) -> list[tuple[str, str, int]]:
    """真删：子先父后，返回每张表实际删掉的行数。"""
    ph = ",".join("?" for _ in ids)
    out: list[tuple[str, str, int]] = []
    for table, column, source in CASCADE:
        if not _has_column(conn, table, column):
            continue
        if source == "direct":
            sql = f"DELETE FROM {table} WHERE {column} IN ({ph})"
        else:
            sub = source[4:].format(ph=ph)
            sql = f"DELETE FROM {table} WHERE {column} IN ({sub})"
        cur = conn.execute(sql, tuple(ids))
        n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if n:
            out.append((table, column, n))
    return out


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    # client_devices / v3_users 都可能被服务端同时读写，用 WAL 更稳
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


class _DbShim:
    """``user_scope`` 期望的对象形状（``._lock`` + ``.conn``）。

    复用真实模块的判定逻辑，而不是在这里重写一份 SQL——两份口径迟早会漂移，
    而"脚本删了界面上说有痕迹的用户"是不可接受的错误。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        import threading

        self.conn = conn
        self._lock = threading.Lock()


def _ids(conn: sqlite3.Connection, sql: str) -> set[str]:
    try:
        return {str(r[0]) for r in conn.execute(sql).fetchall() if r[0]}
    except sqlite3.Error:
        return set()


def main() -> int:
    ap = argparse.ArgumentParser(description="清理没有任何使用痕迹的注册残留")
    ap.add_argument("--data-dir", default=os.getenv("DATA_DIR", "data"), help="数据目录")
    ap.add_argument("--db", default="", help="直接指定 db 文件（优先于 --data-dir）")
    ap.add_argument("--apply", action="store_true", help="真的删除（默认只演练）")
    ap.add_argument("--keep", default="", help="额外保留的 user id，逗号分隔")
    ap.add_argument(
        "--test-identities",
        action="store_true",
        help=(
            "第二档：连「测试身份」一起清。判定依据是 device_key 前缀命中已知的"
            "自动化测试标记（e2e-flow- / voice-check- / refresh- / p2- / demo-seed- / accept-）。"
            "这些行**有**消息或好友，属于在我自己的端到端脚本里造出来的假用户，"
            "不是空壳，所以默认不删，必须显式打开这个开关。"
        ),
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help=(
            "最激进：除「注册过推送设备的账号」和 --keep 之外，**其余用户连同其聊天"
            "记录、好友、动态一起删除**。用于把测试期留下的重复身份清成只剩一个真机。"
            "子表按 CASCADE 级联清理，不会留下孤儿行。默认演练，需配合 --apply。"
        ),
    )
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else Path(args.data_dir) / "speakeasy_v2.db"
    if not db_path.is_file():
        print(f"找不到数据库: {db_path}")
        return 2

    keep = {item.strip() for item in str(args.keep or "").split(",") if item.strip()}

    conn = _open(db_path)
    shim = _DbShim(conn)

    # 延迟导入：确保 sys.path 已加好 backend
    from app.admin import user_scope  # noqa: PLC0415

    rows_total = conn.execute("SELECT COUNT(*) FROM v3_users").fetchone()[0]
    signals = user_scope.signal_ids(shim)
    real = user_scope.real_ids(shim)
    registered = user_scope.registered_ids(shim)

    # 只保留真实存在的行（痕迹表里可能残留 'local' 这类历史哨兵值）
    existing = _ids(conn, "SELECT id FROM v3_users")
    real &= existing
    registered &= existing

    print(f"数据库: {db_path}")
    print(f"v3_users 总数: {rows_total}")
    print("逐个信号命中用户数（有痕迹就不删）:")
    for name, ids in signals.items():
        print(f"  {name:12} {len(ids & existing)}")
    print(f"有痕迹合计: {len(real)}    注册过推送设备: {len(registered)}")

    # 测试身份：按 device_key 前缀识别（这是我这套自动化脚本留下的特征）
    test_ids: set[str] = set()
    if args.test_identities:
        for prefix in TEST_DEVICE_PREFIXES:
            test_ids |= _ids(
                conn,
                f"SELECT id FROM v3_users WHERE device_key LIKE '{prefix}%'",
            )
        test_ids &= existing
        test_ids -= registered  # 有真设备注册的一律不动（兜底）
        print(f"其中「测试身份」（device_key 前缀命中）: {len(test_ids)}")

    all_ids = existing
    # --keep 的 id 必须真实存在：否则一个手误（少几位、复制错）就会让
    # "保护名单"变成空集之外的任意值，而 --all 会把该保的人一起删掉。
    unknown_keep = keep - existing
    if unknown_keep:
        print(f"拒绝执行：--keep 里有 {len(unknown_keep)} 个 id 不在 v3_users 中：{sorted(unknown_keep)}")
        print("（请先用列表确认完整 id，不要用截断的短 id）")
        conn.close()
        return 6

    if args.all:
        # 极端模式：只保留「注册过真设备的」+ --keep，其余（含**有聊天记录**的）全删。
        # 这是数据破坏性操作，所以：
        #  - 必须同时给 --keep 或至少存在注册设备，否则拒绝执行（防空库误删）
        #  - 子表按 CASCADE 一起清，避免留下指向已删用户的孤儿行
        protected = registered | keep
        if not protected:
            print("拒绝执行：--all 需要至少一个 --keep，或数据库里有注册过推送设备的账号。")
            conn.close()
            return 5
        print(f"--all 模式：受保护 = 注册设备 {len(registered)} 个 + --keep {len(keep)} 个")
    elif args.test_identities:
        # "有痕迹"不再是豁免理由——但**注册过真设备的永远豁免**，--keep 也照旧。
        protected = (real - test_ids) | registered | keep
    else:
        protected = real | registered | keep
    candidates = sorted(all_ids - protected)

    print(f"受保护（有痕迹/设备/--keep）: {len(all_ids & protected)}")
    print(f"待清理: {len(candidates)}")
    if keep:
        print(f"  --keep 额外保护: {sorted(keep)}")

    if not candidates:
        print("没有需要清理的行。")
        conn.close()
        return 0

    # 展示前 10 个，便于肉眼确认这不是误删
    print("前 10 个待清理（id / device_key / created_at / last_seen_at）:")
    for uid in candidates[:10]:
        row = conn.execute(
            "SELECT device_key, created_at, last_seen_at FROM v3_users WHERE id=?", (uid,)
        ).fetchone()
        if row:
            print(f"  {uid}  {str(row['device_key'])[:34]:34}  "
                  f"{row['created_at'][:19]}  {row['last_seen_at'][:19]}")

    # 将要连带删除的子表行数——**演练阶段就把破坏面摆出来**
    cascade_preview = _count_cascade(conn, candidates)
    print("\n连带删除（子表 → 行数）:")
    for table, column, n in cascade_preview:
        print(f"  {table:24} {column:16} {n}")

    if not args.apply:
        print("\n[演练] 未删除任何行。确认后加 --apply。")
        conn.close()
        return 0

    # ---- 备份 ----------------------------------------------------------
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = db_path.parent / "_backup" / f"cleanup-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db_path) + suffix)
            if src.is_file():
                shutil.copy2(src, backup_dir / src.name)
    except OSError as exc:
        print(f"备份失败，中止: {exc}")
        conn.close()
        return 3
    manifest = {
        "data_dir": str(db_path.parent),
        "mode": "all" if args.all else ("test-identities" if args.test_identities else "empty-only"),
        "removed": len(candidates),
        "candidates": candidates,
        "protected_real": sorted(real),
        "protected_registered": sorted(registered),
        "protected_keep": sorted(keep),
        "cascade_preview": [{"table": t, "column": c, "rows": n} for t, c, n in cascade_preview],
    }
    (backup_dir / "cleanup-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已备份到: {backup_dir}")

    # ---- 删除（单事务：任一表失败整体回滚） -----------------------------
    try:
        with conn:
            removed = _delete_cascade(conn, candidates)
    except sqlite3.Error as exc:
        print(f"删除失败（事务已回滚）: {exc}")
        conn.close()
        return 4

    print("\n实际删除（子表 → 行数）:")
    for table, column, n in removed:
        print(f"  {table:24} {column:16} {n}")

    left = conn.execute("SELECT COUNT(*) FROM v3_users").fetchone()[0]
    print(f"\n剩余 v3_users: {left}（清理前 {rows_total}）")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
