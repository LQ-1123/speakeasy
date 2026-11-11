"""世界节奏参数接线：保存即生效（不需要重启）。

背景
----
2026-11 前，``life_tick_s`` / ``posts_tick_s`` / ``posts_max_events`` /
``posts_since_hours`` 这 4 个参数在后台能改、能落盘，但 ``maintenance.py``
运行时直接读 ``os.getenv``，从不看词库——**改了等于没改**，旧页面标的
「重启后生效」也是错的（重启同样不生效）。

本文件钉死修复后的行为：
1. ``lexicon.rhythm_params()`` 是唯一取值入口；
2. 优先级 = 环境变量 > 词库文件 > 代码默认；
3. 维护循环每轮重新读取，因此保存后不需要重启；
4. ``RESTART_REQUIRED`` 为空（没有任何节奏参数还要求重启）。
"""

from __future__ import annotations

import json
import os

import pytest

from app.v3 import lexicon


@pytest.fixture()
def clean_rhythm(monkeypatch, tmp_path):
    """把词库目录指到临时目录，并清掉所有节奏相关的环境变量。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import config  # noqa: PLC0415

    config.get_settings.cache_clear()
    for env_name in lexicon.RHYTHM_ENV.values():
        monkeypatch.delenv(env_name, raising=False)
    lexicon._CACHE.clear()  # noqa: SLF001 - 测试需要干净缓存
    yield tmp_path
    lexicon._CACHE.clear()  # noqa: SLF001
    config.get_settings.cache_clear()


def _write_rhythm(tmp_path, params: dict) -> None:
    lex_dir = tmp_path / "lexicons"
    lex_dir.mkdir(parents=True, exist_ok=True)
    (lex_dir / "rhythm.json").write_text(
        json.dumps({"version": 1, "params": params}, ensure_ascii=False), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# 默认值与边界
# --------------------------------------------------------------------------- #


def test_defaults_when_no_lexicon_file(clean_rhythm):
    """词库文件不存在时回落到代码默认值。"""
    params = lexicon.rhythm_params()
    assert params["life_tick_s"] == 300.0
    assert params["posts_tick_s"] == 240.0
    assert params["posts_max_events"] == 4.0
    assert params["posts_since_hours"] == 6.0


def test_all_twelve_params_present(clean_rhythm):
    """10 个参数一个都不能少（少一个运行时就取不到）。"""
    params = lexicon.rhythm_params()
    assert set(params) == set(lexicon.RHYTHM_DEFAULTS)


def test_code_defaults_match_module_constants():
    """``RHYTHM_DEFAULTS`` 必须与各模块的代码常量一致，防止两处漂移。

    这是本文件最重要的一条：``RHYTHM_DEFAULTS`` 是"词库文件缺失时的兜底"，
    一旦它和 ``event_posts`` 里的真实常量不一致，就会出现在"词库文件被人
    删掉"时行为静默改变的问题。
    """
    from app.v3 import event_posts  # noqa: PLC0415

    assert lexicon.RHYTHM_DEFAULTS["event_posts_per_day"] == float(
        event_posts.DEFAULT_EVENT_POSTS_PER_DAY
    )
    assert lexicon.RHYTHM_DEFAULTS["tension_penalty_step"] == pytest.approx(
        event_posts.TENSION_PENALTY_STEP
    )
    assert lexicon.RHYTHM_DEFAULTS["tension_penalty_floor"] == pytest.approx(
        event_posts.TENSION_PENALTY_FLOOR
    )
    assert lexicon.RHYTHM_DEFAULTS["comment_backfill_hours"] == pytest.approx(
        event_posts.COMMENT_BACKFILL_WINDOW_HOURS
    )


# --------------------------------------------------------------------------- #
# 保存即生效（核心回归点）
# --------------------------------------------------------------------------- #


def test_saved_values_take_effect_immediately(clean_rhythm, tmp_path):
    """改了词库文件，**下一次调用**就能读到新值——这是修复的核心。"""
    before = lexicon.rhythm_params()
    assert before["life_tick_s"] == 300.0

    _write_rhythm(
        tmp_path,
        {
            "life_tick_s": 120,
            "posts_tick_s": 90,
            "posts_max_events": 2,
            "posts_since_hours": 3,
        },
    )

    after = lexicon.rhythm_params()
    assert after["life_tick_s"] == 120.0
    assert after["posts_tick_s"] == 90.0
    assert after["posts_max_events"] == 2.0
    assert after["posts_since_hours"] == 3.0


def test_partial_lexicon_keeps_defaults_for_missing_keys(clean_rhythm, tmp_path):
    """只写了部分参数时，其余回落到默认（不能整体失效）。"""
    _write_rhythm(tmp_path, {"life_tick_s": 600})
    params = lexicon.rhythm_params()
    assert params["life_tick_s"] == 600.0
    assert params["posts_tick_s"] == 240.0  # 没写 → 默认


def test_maintenance_loop_reads_rhythm_each_round():
    """``maintenance.py`` 必须用 ``rhythm_params()``，不能再直接读 ``os.getenv``。

    静态断言：这个 bug 的形态是"运行时读环境变量"，光靠行为测试很难覆盖
    （要真跑维护循环）。这里直接检查源码里没有遗留的 ``SE_LIFE_TICK_S`` 等
    直接读取，确保没人把接线改回去。
    """
    import inspect

    from app import maintenance  # noqa: PLC0415

    source = inspect.getsource(maintenance)
    assert "rhythm_params" in source, "维护循环必须通过 rhythm_params() 取节奏参数"
    for env_name in ("SE_LIFE_TICK_S", "SE_POSTS_TICK_S", "SE_POSTS_MAX_EVENTS", "SE_POSTS_SINCE_HOURS"):
        assert f'getenv("{env_name}"' not in source, (
            f"维护循环不应再直接读 {env_name}（那样后台改了不生效）"
        )


# --------------------------------------------------------------------------- #
# 优先级：环境变量 > 词库文件
# --------------------------------------------------------------------------- #


def test_env_overrides_lexicon(clean_rhythm, tmp_path, monkeypatch):
    """环境变量优先级最高（运维紧急刹车不能被后台设置盖掉）。"""
    _write_rhythm(tmp_path, {"life_tick_s": 120})
    monkeypatch.setenv("SE_LIFE_TICK_S", "900")
    assert lexicon.rhythm_params()["life_tick_s"] == 900.0


def test_env_still_works_without_lexicon_file(clean_rhythm, monkeypatch):
    monkeypatch.setenv("SE_POSTS_MAX_EVENTS", "7")
    assert lexicon.rhythm_params()["posts_max_events"] == 7.0


# --------------------------------------------------------------------------- #
# 脏数据防御
# --------------------------------------------------------------------------- #


def test_out_of_bounds_values_are_clamped(clean_rhythm, tmp_path):
    """越界值被夹到合法区间，防止把维护循环拖垮或刷屏。"""
    _write_rhythm(tmp_path, {"life_tick_s": 1, "posts_max_events": 999})
    params = lexicon.rhythm_params()
    low, high = lexicon.RHYTHM_BOUNDS["life_tick_s"]
    assert params["life_tick_s"] == low
    low_e, high_e = lexicon.RHYTHM_BOUNDS["posts_max_events"]
    assert params["posts_max_events"] == high_e


def test_garbage_values_fall_back_to_default(clean_rhythm, tmp_path):
    """非数字（字符串/None）不抛异常，回落默认。"""
    _write_rhythm(tmp_path, {"life_tick_s": "not-a-number", "posts_tick_s": None})
    params = lexicon.rhythm_params()
    assert params["life_tick_s"] == 300.0
    assert params["posts_tick_s"] == 240.0


def test_corrupt_lexicon_file_does_not_raise(clean_rhythm, tmp_path):
    """词库文件是坏 JSON 时，接口仍返回默认值而不是崩掉。"""
    lex_dir = tmp_path / "lexicons"
    lex_dir.mkdir(parents=True, exist_ok=True)
    (lex_dir / "rhythm.json").write_text("{ this is not json", encoding="utf-8")
    params = lexicon.rhythm_params()
    assert params["life_tick_s"] == 300.0


# --------------------------------------------------------------------------- #
# 不再需要重启
# --------------------------------------------------------------------------- #


def test_restart_required_is_empty():
    """没有任何节奏参数还要求重启——4 个 tick 参数已全部接线。"""
    assert lexicon.RESTART_REQUIRED == (), (
        "tick 参数已改为保存即生效，不该再出现在 RESTART_REQUIRED 里"
    )


def test_stats_snapshot_uses_same_source(clean_rhythm, tmp_path):
    """调试快照的节奏值与运行期同源（避免"界面显示一套、实际跑另一套"）。"""
    from app.v3 import service  # noqa: PLC0415

    _write_rhythm(tmp_path, {"life_tick_s": 180, "posts_max_events": 6})
    snapshot = service._rhythm_snapshot()  # noqa: SLF001
    assert snapshot["life_tick_s"] == 180.0
    assert snapshot["posts_max_events"] == 6
    # 类型也要对：前端按数字渲染
    assert isinstance(snapshot["posts_max_events"], int)
