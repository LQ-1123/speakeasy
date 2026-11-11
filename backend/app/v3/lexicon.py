"""词库（lexicon）：捏人/表达的**可管理词表**，与客户端完全解耦。

为什么要抽出来
--------------
角色的表达由一堆"词表"决定：DNA 枚举值（以及每个值对应的行为数值）、示例场景、
口头禅池、禁用词、兴趣/名字/职业池。这些东西以前散落在代码常量里，
只有改代码才能调——运营无法自助，也无从知道"哪个词根本没被用上"。

本模块把它们变成 ``data/lexicons/*.json``：

* **加载**：``load(name)``，按 mtime 缓存，文件不存在时返回 None（代码默认值继续生效）；
* **校验**：``validate(name, data)`` 检查引用完整性（例如每个 DNA 枚举值必须带数值映射、
  默认值必须存在、场景不能重复）；
* **保存**：``save(name, data)`` 先校验再原子写（临时文件 + replace），并保留上一版 ``*.bak.json``；
* **生效**：``refresh()`` 把词库应用回运行时（目前接的是 Speech DNA：枚举、数值映射、默认值、场景）；
* **统计**：``usage_stats(db)`` 算出每个词条被多少角色使用（找出死词、发现撞梗）。

设计约束
--------
* 词表**只描述参数与素材**，不描述拼装逻辑（prompt 结构仍在代码里，保证护栏不被改坏）；
* 枚举值**只能停用、不能删除**：已有角色的 DNA 可能还在用，删除会造成非法值；
* 所有函数都不抛异常给调用方崩溃路径（除 ``save`` 的校验错误外），失败只降级为"用代码默认值"。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from typing import Any

from app.logging_utils import get_logger

log = get_logger(__name__)

#: 词库文件 → 中文名（后台展示用）
LEXICON_FILES: dict[str, str] = {
    "dna": "表达 DNA：枚举值 + 行为数值映射",
    "scenes": "示例对话场景",
    "phrases": "口头禅池 / 全局禁用词",
    "events": "AI 事件类型 / 价值档 / 分享权重",
    "rhythm": "世界节奏：事件与动态的产出参数",
    "world": "捏人素材池：名字 / 职业 / 城市 / 兴趣",
}

#: mtime 缓存
_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}

#: 上一次应用词库后的摘要（后台/健康检查用）
_APPLIED: dict[str, Any] = {
    "at": 0.0,
    "dna_fields": 0,
    "scenes": 0,
    "banned": 0,
    "event_types": 0,
    "rhythm": 0,
    "error": "",
}

#: rhythm 参数的合法范围（保存时强制；同时是"模拟今天"的默认猜测边界）
RHYTHM_BOUNDS: dict[str, tuple[float, float]] = {
    "rule_events_per_day": (1, 8),
    "narrative_events_per_day": (0, 6),
    "event_posts_per_day": (1, 30),
    "tension_penalty_step": (0.0, 0.5),
    "tension_penalty_floor": (0.0, 0.8),
    "comment_backfill_hours": (0.0, 24.0),
    "life_tick_s": (60.0, 3600.0),
    "posts_tick_s": (60.0, 3600.0),
    "posts_max_events": (1, 20),
    "posts_since_hours": (1.0, 48.0),
}

#: 需要重启才生效的参数。
#:
#: 2026-11 起为**空**：4 个 tick/批量参数（``life_tick_s`` / ``posts_tick_s`` /
#: ``posts_max_events`` / ``posts_since_hours``）已接入 :func:`rhythm_params`，
#: 维护循环每轮重新读取，所以后台保存后**下一轮即生效**，不需要重启。
#:
#: 历史说明：旧实现在 ``maintenance.py`` 里直接读 ``os.getenv``，这 4 个参数
#: 改了从来没生效过——旧页面标的「重启后生效」也是错的（重启同样不生效）。
#: 保留这个常量，是为了给将来的新参数留一个统一的"要不要重启"出口。
RESTART_REQUIRED: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def lexicon_dir() -> str:
    """词库目录：``<data_path>/lexicons``（不存在会自动创建）。"""
    try:
        from app.config import get_settings  # noqa: PLC0415

        base = str(get_settings().data_path)
    except Exception:  # noqa: BLE001 - 测试/异常环境
        base = "data"
    path = os.path.join(base, "lexicons")
    os.makedirs(path, exist_ok=True)
    return path


def path_for(name: str) -> str:
    filename = LEXICON_FILES.get(name) and f"{name}.json"
    if not filename or name not in LEXICON_FILES:
        raise KeyError(f"未知词库: {name}")
    return os.path.join(lexicon_dir(), filename)


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #
def load(name: str, *, fresh: bool = False) -> dict[str, Any] | None:
    """读取词库；文件不存在/解析失败返回 None（调用方用代码默认值）。"""
    try:
        path = path_for(name)
    except KeyError:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cache_key = (name, path)
    hit = _CACHE.get(cache_key)
    if not fresh and hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as fh:  # noqa: PTH123
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("词库根节点必须是对象")
    except Exception as exc:  # noqa: BLE001
        log.warning("词库 %s 读取失败（忽略，用代码默认值）: %s", name, exc)
        _CACHE[cache_key] = (mtime, None)
        return None
    _CACHE[cache_key] = (mtime, data)
    return data


def load_all() -> dict[str, dict[str, Any] | None]:
    return {name: load(name) for name in LEXICON_FILES}


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate(name: str, data: Any) -> list[str]:
    """返回问题列表（空列表 = 通过）。**只校验，不改数据。**"""
    if name not in LEXICON_FILES:
        return [f"未知词库: {name}"]
    if not isinstance(data, dict):
        return ["词库根节点必须是 JSON 对象"]
    if name == "dna":
        return _validate_dna(data)
    if name == "scenes":
        return _validate_scenes(data)
    if name == "phrases":
        return _validate_phrases(data)
    if name == "events":
        return _validate_events(data)
    if name == "rhythm":
        return _validate_rhythm(data)
    if name == "world":
        return _validate_world(data)
    return []


def _validate_dna(data: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    fields = data.get("fields")
    if not isinstance(fields, dict) or not fields:
        return ["dna.fields 必须是非空对象（字段 → 取值）"]
    if not _is_number(data.get("version")):
        issues.append("dna.version 必须是数字")
    for field, spec in fields.items():
        if not isinstance(spec, dict):
            issues.append(f"dna.fields.{field} 必须是对象")
            continue
        values = spec.get("values")
        if not isinstance(values, dict) or not values:
            issues.append(f"dna.fields.{field}.values 必须是非空对象")
            continue
        enabled = [k for k, v in values.items() if isinstance(v, dict) and v.get("enabled", True)]
        if not enabled:
            issues.append(f"dna.fields.{field} 至少要有一个启用的取值")
        for value, meta in values.items():
            if not isinstance(meta, dict):
                issues.append(f"dna.fields.{field}.values.{value} 必须是对象")
                continue
            trait = meta.get("trait")
            if trait is not None and (not _is_number(trait) or not 0.0 <= float(trait) <= 1.0):
                issues.append(
                    f"dna.fields.{field}.values.{value}.trait 必须在 0..1（当前 {trait!r}）"
                )
        default = spec.get("default")
        if default is not None and default not in values:
            issues.append(f"dna.fields.{field}.default={default!r} 不在取值列表里")
        elif default is not None and not values.get(default, {}).get("enabled", True):
            issues.append(f"dna.fields.{field}.default={default!r} 已被停用")
    return issues


def _validate_scenes(data: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return ["scenes.scenes 必须是非空数组"]
    seen: set[str] = set()
    for item in scenes:
        key = item.get("key") if isinstance(item, dict) else item
        if not isinstance(key, str) or not key.strip():
            issues.append("每个场景必须是字符串或带 key 的对象")
            continue
        if key in seen:
            issues.append(f"场景重复: {key}")
        seen.add(key)
    if len(seen) < 8:
        issues.append(f"场景数 {len(seen)} 太少（示例覆盖至少要 8 个）")
    return issues


def _validate_phrases(data: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    banned = data.get("banned_global")
    if banned is None:
        issues.append("phrases.banned_global 缺失（可为空数组）")
    elif not isinstance(banned, list) or any(not str(x).strip() for x in banned):
        issues.append("phrases.banned_global 必须是非空字符串数组")
    pools = data.get("signature_pools")
    if pools is not None:
        if not isinstance(pools, dict):
            issues.append("phrases.signature_pools 必须是 语言 → 数组")
        else:
            for lang, items in pools.items():
                if not isinstance(items, list) or any(not str(x).strip() for x in items):
                    issues.append(f"phrases.signature_pools.{lang} 必须是非空字符串数组")
                elif len(items) > 80:
                    issues.append(f"phrases.signature_pools.{lang} 超过 80 条，容易撞梗")
    return issues


def _validate_world(data: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    names = data.get("names")
    if isinstance(names, dict):
        for group, items in names.items():
            if not isinstance(items, list):
                issues.append(f"world.names.{group} 必须是数组")
                continue
            for index, item in enumerate(items):
                if isinstance(item, str):
                    if not item.strip():
                        issues.append(f"world.names.{group}[{index}] 不能为空")
                    continue
                if not isinstance(item, dict):
                    issues.append(f"world.names.{group}[{index}] 必须是字符串或 {{name, gender, country}}")
                    continue
                if not str(item.get("name") or "").strip():
                    issues.append(f"world.names.{group}[{index}].name 不能为空")
                gender = item.get("gender")
                if gender not in (None, "", "male", "female"):
                    issues.append(f"world.names.{group}[{index}].gender 只能是 male / female")
                country = item.get("country")
                if country is not None and not isinstance(country, str):
                    issues.append(f"world.names.{group}[{index}].country 必须是字符串")
    for key in ("occupations", "cities", "interests"):
        pool = data.get(key, {})
        if not isinstance(pool, dict):
            issues.append(f"world.{key} 必须是 语言/分类 → 数组")
            continue
        for group, items in pool.items():
            if not isinstance(items, list) or any(not str(x).strip() for x in items):
                issues.append(f"world.{key}.{group} 必须是非空字符串数组")
    if not issues and not any(data.get(k) for k in ("names", "occupations", "cities", "interests")):
        issues.append("world 至少要有一类素材池")
    return issues



def _validate_events(data: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    types = data.get("types")
    if not isinstance(types, dict) or not types:
        return ["events.types 必须是非空对象（事件类型 → 配置）"]
    enabled: list[str] = []
    for key, spec in types.items():
        if not isinstance(spec, dict):
            issues.append(f"events.types.{key} 必须是对象")
            continue
        tier = spec.get("value_tier", 0)
        if not isinstance(tier, int) or isinstance(tier, bool) or not 0 <= tier <= 3:
            issues.append(f"events.types.{key}.value_tier 必须是 0..3 的整数（当前 {tier!r}）")
        source = spec.get("source")
        if source not in ("rule", "narrative", "both"):
            issues.append(f"events.types.{key}.source 必须是 rule / narrative / both")
        weight = spec.get("share_weight", 0.7)
        if not _is_number(weight) or not 0.0 <= float(weight) <= 2.0:
            issues.append(f"events.types.{key}.share_weight 必须在 0..2（它是倍率，不是概率；当前 {weight!r}）")
        if "show_title" in spec and not isinstance(spec["show_title"], bool):
            issues.append(f"events.types.{key}.show_title 必须是 true/false")
        tags = spec.get("tags", [])
        if not isinstance(tags, list) or any(not str(t).strip() for t in tags):
            issues.append(f"events.types.{key}.tags 必须是非空字符串数组")
        if spec.get("enabled", True):
            enabled.append(key)
    if not enabled:
        issues.append("至少要有一个启用的事件类型")
        return issues
    if not any(types[k].get("source") in ("rule", "both") for k in enabled):
        issues.append("至少要有一个「规则来源」的事件类型（否则世界不会自动产生日常）")
    if not any(types[k].get("source") in ("narrative", "both") for k in enabled):
        issues.append("至少要有一个「叙事来源」的事件类型（否则没有故事性事件）")
    return issues


def _validate_rhythm(data: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    params = data.get("params")
    if not isinstance(params, dict):
        return ["rhythm.params 必须是对象"]
    for key, (low, high) in RHYTHM_BOUNDS.items():
        if key not in params:
            issues.append(f"缺少参数 {key}")
            continue
        value = params[key]
        if not _is_number(value):
            issues.append(f"rhythm.params.{key} 必须是数字（当前 {value!r}）")
            continue
        if not low <= float(value) <= high:
            issues.append(f"rhythm.params.{key} 必须在 {low}..{high}（当前 {value}）")
    return issues


def validate_all() -> dict[str, list[str]]:
    return {name: validate(name, data) for name, data in load_all().items() if data is not None}


# --------------------------------------------------------------------------- #
# 保存（原子写 + 备份）
# --------------------------------------------------------------------------- #
class LexiconError(ValueError):
    """保存失败（校验不通过）。``issues`` 里是具体原因。"""

    def __init__(self, message: str, issues: list[str] | None = None) -> None:
        super().__init__(message)
        self.issues = issues or []


def save(name: str, data: dict[str, Any], *, validate_first: bool = True) -> dict[str, Any]:
    """校验 → 备份旧版 → 原子写 → 清缓存 → 应用。返回 ``{'path', 'backup'}``。"""
    if name not in LEXICON_FILES:
        raise LexiconError(f"未知词库: {name}")
    issues = validate(name, data) if validate_first else []
    if issues:
        raise LexiconError(f"{name} 校验未通过", issues)
    path = path_for(name)
    backup = ""
    try:
        if os.path.exists(path):
            backup = path[: -len(".json")] + ".bak.json"
            shutil.copyfile(path, backup)
    except OSError as exc:  # noqa: BLE001
        log.warning("词库备份失败（继续保存）: %s", exc)
        backup = ""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:  # noqa: PTH123
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    _CACHE.pop((name, path), None)
    refresh()
    return {"path": path, "backup": backup}


def reset_to_default(name: str) -> dict[str, Any]:
    """回滚到上一版（``*.bak.json``）；没有备份则删除当前文件 = 回到代码默认值。"""
    path = path_for(name)
    backup = path[: -len(".json")] + ".bak.json"
    if os.path.exists(backup):
        os.replace(backup, path)
        _CACHE.pop((name, path), None)
        refresh()
        return {"restored": "backup"}
    if os.path.exists(path):
        os.remove(path)  # noqa: PTH107
        _CACHE.pop((name, path), None)
        refresh()
        return {"restored": "code-default"}
    _CACHE.pop((name, path), None)
    refresh()
    return {"restored": "none"}


# --------------------------------------------------------------------------- #
# 应用到运行时
# --------------------------------------------------------------------------- #
def refresh() -> dict[str, Any]:
    """把词库应用回运行时（DNA / 场景 / 事件类型 / 世界节奏）。失败只记日志。"""
    summary: dict[str, Any] = {
        "at": time.time(),
        "dna_fields": 0,
        "scenes": 0,
        "banned": 0,
        "event_types": 0,
        "rhythm": 0,
        "error": "",
    }
    try:
        from app.v3 import speech_dna  # noqa: PLC0415

        applied = speech_dna.refresh_from_lexicon()
        summary["dna_fields"] = int(applied.get("dna_fields") or 0)
        summary["scenes"] = int(applied.get("scenes") or 0)
        summary["banned"] = int(applied.get("banned") or 0)
    except Exception as exc:  # noqa: BLE001
        summary["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("应用 DNA 词库失败（继续用代码默认值）: %s", exc)
    try:
        from app.v3 import life_events  # noqa: PLC0415

        applied = life_events.refresh_from_lexicon()
        summary["event_types"] = max(summary["event_types"], int(applied.get("event_types") or 0))
    except Exception as exc:  # noqa: BLE001
        log.warning("应用事件词库失败（继续用代码默认值）: %s", exc)
    try:
        from app.v3 import event_posts  # noqa: PLC0415

        applied = event_posts.refresh_from_lexicon()
        summary["event_types"] = max(summary["event_types"], int(applied.get("event_types") or 0))
        summary["rhythm"] = int(applied.get("rhythm") or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("应用世界节奏词库失败（继续用代码默认值）: %s", exc)
    _APPLIED.clear()
    _APPLIED.update(summary)
    return summary


def applied_status() -> dict[str, Any]:
    return dict(_APPLIED)


# --------------------------------------------------------------------------- #
# 使用统计（找出死词 / 撞梗）
#: rhythm 参数的代码默认值（词库文件缺失/字段缺失时用它）。
#: 运行时优先级：**环境变量 > 词库文件 > 这里的默认值**。
#: 环境变量仍然优先，是为了给运维留一个不改词库就能临时压制的手段。
#:
#: 注意：这些值必须与各模块的代码常量保持一致（``event_posts`` 的
#: ``TENSION_PENALTY_STEP`` / ``TENSION_PENALTY_FLOOR`` /
#: ``DEFAULT_EVENT_POSTS_PER_DAY`` / ``COMMENT_BACKFILL_WINDOW_HOURS``，
#: 以及 ``life_events`` 的事件配额）。测试 ``test_lexicon_rhythm_wiring``
#: 会断言两者相等，防止这里悄悄漂移。
RHYTHM_DEFAULTS: dict[str, float] = {
    "rule_events_per_day": 4.0,
    "narrative_events_per_day": 2.0,
    "event_posts_per_day": 8.0,
    "tension_penalty_step": 0.18,
    "tension_penalty_floor": 0.15,
    "comment_backfill_hours": 6.0,
    "life_tick_s": 300.0,
    "posts_tick_s": 240.0,
    "posts_max_events": 4.0,
    "posts_since_hours": 6.0,
}

#: rhythm 参数 → 对应的环境变量名（运维覆盖通道，优先级最高）。
RHYTHM_ENV: dict[str, str] = {
    "rule_events_per_day": "SE_RULE_EVENTS_PER_DAY",
    "narrative_events_per_day": "SE_NARRATIVE_EVENTS_PER_DAY",
    "event_posts_per_day": "SE_EVENT_POSTS_PER_DAY",
    "tension_penalty_step": "SE_TENSION_PENALTY_STEP",
    "tension_penalty_floor": "SE_TENSION_PENALTY_FLOOR",
    "comment_backfill_hours": "SE_COMMENT_BACKFILL_HOURS",
    "life_tick_s": "SE_LIFE_TICK_S",
    "posts_tick_s": "SE_POSTS_TICK_S",
    "posts_max_events": "SE_POSTS_MAX_EVENTS",
    "posts_since_hours": "SE_POSTS_SINCE_HOURS",
}


def rhythm_params(*, fresh: bool = False) -> dict[str, float]:
    """当前生效的世界节奏参数（**每次调用都读最新值**，保存即生效）。

    为什么要有这个函数
    ------------------
    2026-11 前，``life_tick_s`` / ``posts_tick_s`` / ``posts_max_events`` /
    ``posts_since_hours`` 这 4 个参数在后台能改、能落盘，但**运行时从来没人读**
    （``maintenance.py`` 直接读 ``os.getenv``），所以改了等于没改，旧页面标的
    「重启后生效」也是错的——重启也不会生效。

    现在统一从这里取：维护循环每轮都调一次，运营在后台保存后**下一轮就生效**，
    不需要重启。

    优先级：环境变量 > 词库文件 > :data:`RHYTHM_DEFAULTS`。
    环境变量优先是为了保住运维的紧急刹车；它一旦设置就会盖过后台设置。
    """
    raw = (load("rhythm", fresh=fresh) or {}).get("params") or {}
    out: dict[str, float] = {}
    for key, default in RHYTHM_DEFAULTS.items():
        value = default
        # 1) 词库文件
        if isinstance(raw, dict) and key in raw:
            try:
                value = float(raw[key])
            except (TypeError, ValueError):
                value = default
        # 2) 环境变量（最高优先级）
        env_name = RHYTHM_ENV.get(key)
        if env_name:
            flag = os.getenv(env_name)
            if flag not in (None, ""):
                try:
                    value = float(flag)
                except (TypeError, ValueError):
                    pass
        # 3) 夹到合法区间，防止脏数据把循环拖垮
        bounds = RHYTHM_BOUNDS.get(key)
        if bounds:
            value = max(float(bounds[0]), min(float(bounds[1]), value))
        out[key] = value
    return out


# --------------------------------------------------------------------------- #
def usage_stats(db: Any) -> dict[str, Any]:
    """统计每个词条被多少角色使用；失败返回空结构。"""
    stats: dict[str, Any] = {"dna": {}, "scenes": {}, "personas": 0}
    if db is None:
        return stats
    try:
        rows = db.conn.execute("SELECT speech_dna FROM personas").fetchall()
    except Exception as exc:  # noqa: BLE001
        log.debug("统计词库使用失败: %s", exc)
        return stats
    dna_dir = load("dna") or {}
    fields = dna_dir.get("fields") or {}
    counters: dict[str, dict[str, int]] = {field: {} for field in fields}
    personas = 0
    for row in rows:
        raw = row["speech_dna"]
        if not raw:
            continue
        try:
            dna = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        if not isinstance(dna, dict):
            continue
        personas += 1
        for field in fields:
            value = str(dna.get(field) or "")
            if value:
                counters[field][value] = counters[field].get(value, 0) + 1
    stats["personas"] = personas
    stats["dna"] = counters
    try:
        world = load("world") or {}
        stats["world"] = {
            category: {group: len(items) for group, items in (pools or {}).items()}
            for category, pools in world.items()
            if isinstance(pools, dict)
        }
    except Exception:  # noqa: BLE001
        stats["world"] = {}
    try:
        scene_rows = db.conn.execute(
            "SELECT scene, COUNT(*) AS n FROM persona_example_dialogs GROUP BY scene"
        ).fetchall()
        stats["scenes"] = {str(r["scene"]): int(r["n"]) for r in scene_rows}
    except Exception:  # noqa: BLE001
        stats["scenes"] = {}
    return stats



# --------------------------------------------------------------------------- #
# 模拟今天的产出（只读、不调 LLM、不落库）
# --------------------------------------------------------------------------- #
def simulate_today(db: Any) -> dict[str, Any]:
    """按当前词库参数 + 世界现状，估算"今天会产出多少事件/动态/评论"。

    这是给运营用的**预估**，不是精确值：事件生成有随机性、LLM 可能失败、
    分享概率还受情绪/亲近度影响。所有假设都会回传，避免被当成事实。
    """
    out: dict[str, Any] = {
        "params": {},
        "types": {},
        "world": {},
        "today": {},
        "pending": {},
        "next_day": {},
        "assumptions": [
            "事件生成带随机性，这里给的是量级估计，不是承诺",
            "分享概率按「已完成且尚未发动态」的事件实际计算，取平均后乘以数量",
            "评论数按每条约 0.8 条评论的历史经验估算",
        ],
    }
    rhythm = (load("rhythm") or {}).get("params") or {}
    out["params"] = rhythm
    types = (load("events") or {}).get("types") or {}
    out["types"] = {
        "total": len(types),
        "enabled": sum(1 for v in types.values() if isinstance(v, dict) and v.get("enabled", True)),
    }
    if db is None:
        out["assumptions"].append("没有数据库连接，无法估算世界现状")
        return out
    try:
        from app.v3 import event_posts, life_events  # noqa: PLC0415
        from app.v3 import speech_dna  # noqa: PLC0415

        conn = db.conn
        personas = int(
            (conn.execute("SELECT COUNT(*) AS n FROM personas").fetchone() or {"n": 0})["n"] or 0
        )
        out["world"] = {
            "personas": personas,
            "rule_types": len(getattr(life_events, "RULE_EVENT_TYPES", ())),
            "narrative_types": len(getattr(life_events, "NARRATIVE_EVENT_TYPES", ())),
        }
        today_rows = conn.execute(
            "SELECT source_type, COUNT(*) AS n FROM persona_events"
            " WHERE day=date('now','localtime') GROUP BY source_type"
        ).fetchall()
        out["today"]["events"] = {str(r["source_type"]): int(r["n"]) for r in today_rows}
        posts = conn.execute(
            "SELECT COUNT(*) AS n FROM persona_posts WHERE substr(created_at,1,10)=date('now','localtime')"
        ).fetchone()
        comments = conn.execute(
            "SELECT COUNT(*) AS n FROM persona_post_comments"
            " WHERE substr(created_at,1,10)=date('now','localtime')"
        ).fetchone()
        out["today"]["posts"] = int((posts or {"n": 0})["n"] or 0)
        out["today"]["comments"] = int((comments or {"n": 0})["n"] or 0)

        eligible = conn.execute(
            "SELECT e.id, e.persona_id, e.event_type, e.visibility, e.event_status, e.created_at, e.metadata"
            " FROM persona_events e"
            " WHERE e.event_status='completed' AND NOT EXISTS ("
            "   SELECT 1 FROM persona_posts p WHERE p.source_event_id = e.id)"
            " ORDER BY e.created_at DESC LIMIT 300"
        ).fetchall()
        personas_cache: dict[str, dict] = {}
        probs: list[float] = []
        for row in eligible[:200]:
            pid = str(row["persona_id"] or "")
            persona = personas_cache.get(pid)
            if persona is None:
                raw = conn.execute("SELECT * FROM personas WHERE id=?", (pid,)).fetchone()
                persona = dict(raw) if raw is not None else {}
                personas_cache[pid] = persona
            if not persona:
                continue
            try:
                dna = json.loads(str(persona.get("speech_dna") or "{}"))
            except (TypeError, ValueError):
                dna = {}
            try:
                probs.append(event_posts.share_probability(persona, speech_dna.dna_traits(dna), dict(row)))
            except Exception:  # noqa: BLE001
                probs.append(0.5)
        avg = sum(probs) / len(probs) if probs else 0.5
        quota = int(float(rhythm.get("event_posts_per_day") or event_posts.DEFAULT_EVENT_POSTS_PER_DAY))
        expected = min(quota, int(round(len(eligible) * avg)))
        out["pending"] = {
            "eligible_events": len(eligible),
            "avg_share_prob": round(avg, 3),
            "quota": quota,
            "expected_new_posts": expected,
            "expected_new_comments": int(round(expected * 0.8)),
        }
        rules = int(float(rhythm.get("rule_events_per_day") or 4))
        narrative = int(float(rhythm.get("narrative_events_per_day") or 0))
        out["next_day"] = {
            "rule_events_est": rules * personas,
            "narrative_events_est": narrative,
            "posts_quota": quota,
            "events_total_est": rules * personas + narrative,
        }
    except Exception as exc:  # noqa: BLE001
        out["assumptions"].append(f"估算失败：{type(exc).__name__}: {exc}")
    return out


__all__ = [
    "RHYTHM_BOUNDS",
    "RESTART_REQUIRED",
    "LEXICON_FILES",
    "LexiconError",
    "applied_status",
    "lexicon_dir",
    "load",
    "load_all",
    "path_for",
    "refresh",
    "reset_to_default",
    "save",
    "simulate_today",
    "usage_stats",
    "validate",
    "validate_all",
]
