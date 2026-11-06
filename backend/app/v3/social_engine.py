"""SpeakEasy V3 · 社交事件引擎（纯逻辑，可注入随机源）。

产品规则（对应任务书 §2.4）
--------------------------
1. 先看用户动态的**主题/标签**，再从 Persona 池里找人（兴趣重合度打分）。
2. 有候选也**不一定**有事件：``base_probability`` 先掷一次。
3. 事件类型按权重抽（like / comment / friend_request）。
4. 事件**延迟**出现：``scheduled_at = now + uniform(delay_min, delay_max)``。
5. 同一人物对同一用户有**冷却**（不会连续轰炸）。
6. 同一条动态最多 ``max_events_per_post`` 个事件。
7. 每个用户 24h 内最多 ``max_events_per_user_day`` 个事件。
8. **允许什么都不发生**（返回空列表，调用方记 ``no_action`` 供统计）。
9. 重复触发同一动态不会产生重复事件（由 service 层的 evaluated 标记 + dedupe_key 保证）。

本模块不碰数据库、不调模型，只用传入的 ``rng``；因此自动化测试可以
``random.Random(42)`` 复现完全相同的判定。
"""

from __future__ import annotations

import hashlib
import os
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.v3.seed_data import INTEREST_KEYWORDS

#: 事件类型
EVENT_TYPES: tuple[str, ...] = ("like", "comment", "friend_request")

#: 可见性：只有公开动态会进入公共社交事件
PUBLIC_VISIBILITY = "public"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _parse_weights(raw: str) -> dict[str, float]:
    weights: dict[str, float] = {}
    for chunk in str(raw or "").split(","):
        if not chunk.strip() or ":" not in chunk:
            continue
        key, _, value = chunk.partition(":")
        key = key.strip().lower()
        if key not in EVENT_TYPES:
            continue
        try:
            weights[key] = max(0.0, float(value))
        except ValueError:
            continue
    return weights


@dataclass(frozen=True)
class SocialConfig:
    """事件引擎的可配置参数（全部可用环境变量覆盖）。"""

    enabled: bool = True
    base_probability: float = 0.55
    type_weights: dict[str, float] = field(
        default_factory=lambda: {"like": 0.55, "comment": 0.32, "friend_request": 0.13}
    )
    delay_min_s: int = 5
    delay_max_s: int = 60
    persona_cooldown_s: int = 6 * 3600
    max_events_per_post: int = 2
    max_events_per_user_day: int = 6
    max_candidates: int = 3
    min_match_score: int = 1

    @classmethod
    def from_env(cls) -> SocialConfig:
        raw_enabled = os.getenv("SE_SOCIAL_ENABLED", "1").strip().lower()
        weights = _parse_weights(os.getenv("SE_SOCIAL_WEIGHTS", ""))
        cfg = cls(
            enabled=raw_enabled not in ("0", "false", "off", "no"),
            base_probability=min(1.0, max(0.0, _env_float("SE_SOCIAL_BASE_PROB", 0.55))),
            type_weights=weights or {"like": 0.55, "comment": 0.32, "friend_request": 0.13},
            delay_min_s=max(0, _env_int("SE_SOCIAL_DELAY_MIN_S", 5)),
            delay_max_s=max(0, _env_int("SE_SOCIAL_DELAY_MAX_S", 60)),
            persona_cooldown_s=max(0, _env_int("SE_SOCIAL_PERSONA_COOLDOWN_S", 6 * 3600)),
            max_events_per_post=max(1, _env_int("SE_SOCIAL_MAX_PER_POST", 2)),
            max_events_per_user_day=max(0, _env_int("SE_SOCIAL_MAX_PER_USER_DAY", 6)),
            max_candidates=max(1, _env_int("SE_SOCIAL_MAX_CANDIDATES", 3)),
            min_match_score=max(0, _env_int("SE_SOCIAL_MIN_MATCH_SCORE", 1)),
        )
        return cfg

    def public(self) -> dict[str, Any]:
        """给前端/测试看的安全配置视图。"""
        return {
            "enabled": self.enabled,
            "base_probability": self.base_probability,
            "type_weights": dict(self.type_weights),
            "delay_min_s": self.delay_min_s,
            "delay_max_s": self.delay_max_s,
            "persona_cooldown_s": self.persona_cooldown_s,
            "max_events_per_post": self.max_events_per_post,
            "max_events_per_user_day": self.max_events_per_user_day,
            "max_candidates": self.max_candidates,
            "min_match_score": self.min_match_score,
        }


# --------------------------------------------------------------------------- #
# 主题匹配
# --------------------------------------------------------------------------- #
def extract_tags(
    text: str,
    explicit_tags: Iterable[str] | None = None,
    profile_interests: Iterable[str] | None = None,
) -> list[str]:
    """从文本 + 显式标签里提取标准兴趣标签（英文 canonical）。

    - 显式标签优先，原样小写去空；不认识的也保留（用户自己打的标签有意义）
    - 文本里按 ``INTEREST_KEYWORDS`` 做子串匹配（中英文都认）
    - 用户画像兴趣作为弱信号加入（不重复）
    """
    tags: list[str] = []

    def add(tag: str) -> None:
        tag = str(tag or "").strip().lower()
        if tag and tag not in tags:
            tags.append(tag)

    for tag in explicit_tags or []:
        for piece in str(tag).replace("，", ",").split(","):
            add(piece)

    low = str(text or "").lower()
    if low:
        for canonical, keywords in INTEREST_KEYWORDS.items():
            if canonical in tags:
                continue
            if any(kw and kw.lower() in low for kw in keywords):
                add(canonical)

    for tag in profile_interests or []:
        add(tag)

    return tags


def match_score(persona: dict[str, Any], tags: Sequence[str]) -> int:
    """兴趣重合度：每个共同标签 2 分。"""
    interests = {str(x).strip().lower() for x in (persona.get("interests") or [])}
    if not interests:
        return 0
    overlap = interests & {str(t).strip().lower() for t in tags}
    return len(overlap) * 2


def rank_candidates(
    personas: Sequence[dict[str, Any]],
    tags: Sequence[str],
    *,
    is_friend: Any = None,
    limit: int = 3,
    min_score: int = 1,
) -> list[dict[str, Any]]:
    """按匹配度挑候选人物，返回 ``[{persona, score, is_friend}]``。

    平局时用 persona id 的稳定哈希排序，保证同一输入永远同一顺序（可测）。
    """
    scored: list[tuple[int, str, dict[str, Any], bool]] = []
    for persona in personas:
        if str(persona.get("status") or "active") != "active":
            continue
        score = match_score(persona, tags)
        if score < min_score:
            continue
        pid = str(persona.get("id") or "")
        friend = bool(is_friend(persona)) if callable(is_friend) else False
        tie = int(hashlib.sha256(pid.encode("utf-8")).hexdigest()[:8], 16)
        scored.append((score, f"{tie:08x}", persona, friend))

    scored.sort(key=lambda item: (-item[0], item[1]))
    out: list[dict[str, Any]] = []
    for score, _tie, persona, friend in scored[: max(1, limit)]:
        out.append({"persona": persona, "score": score, "is_friend": friend})
    return out


# --------------------------------------------------------------------------- #
# 概率判定
# --------------------------------------------------------------------------- #
@dataclass
class PlannedEvent:
    """一个「将要发生」的事件（还没写库）。"""

    persona_id: str
    event_type: str
    delay_s: int
    roll: float
    score: int = 0
    is_friend: bool = False
    reason: str = ""


def _pick_type(
    rng: random.Random, weights: dict[str, float], *, allow_friend_request: bool
) -> tuple[str, float]:
    pool = {
        key: max(0.0, float(val))
        for key, val in weights.items()
        if key in EVENT_TYPES and (key != "friend_request" or allow_friend_request)
    }
    total = sum(pool.values())
    if total <= 0:
        return "like", 1.0
    roll = rng.uniform(0.0, total)
    acc = 0.0
    for key in EVENT_TYPES:  # 固定顺序，保证可复现
        weight = pool.get(key, 0.0)
        if weight <= 0:
            continue
        acc += weight
        if roll <= acc:
            return key, roll
    return "like", roll


def plan_events(
    candidates: Sequence[dict[str, Any]],
    *,
    rng: random.Random,
    config: SocialConfig,
    match_weights: dict[str, float] | None = None,
    remaining_slots: int | None = None,
) -> tuple[list[PlannedEvent], list[dict[str, Any]]]:
    """把候选人物变成「计划事件」。

    返回 ``(planned, attempts)``：``attempts`` 里记录每个候选的判定过程
    （``event_type='no_action'`` + reason），用于统计与调参。

    规则：每个候选独立掷一次 ``base_probability``（按匹配度略微加权），
    命中的候选再抽事件类型；总数受 ``remaining_slots`` 限制。
    """
    planned: list[PlannedEvent] = []
    attempts: list[dict[str, Any]] = []
    if not candidates:
        attempts.append({"persona_id": "", "event_type": "no_action", "reason": "no_candidate", "roll": 0.0})
        return planned, attempts

    slots = config.max_events_per_post if remaining_slots is None else max(0, remaining_slots)
    max_score = max((int(c.get("score") or 0) for c in candidates), default=0) or 1

    for candidate in candidates:
        if len(planned) >= slots:
            break
        persona = candidate.get("persona") or {}
        pid = str(persona.get("id") or "")
        score = int(candidate.get("score") or 0)
        is_friend = bool(candidate.get("is_friend"))
        # 匹配度越高，命中概率越高：0.6 + 0.4 * (score / max_score)
        factor = 0.6 + 0.4 * (min(score, max_score) / max_score)
        probability = min(1.0, max(0.0, config.base_probability * factor))
        roll = rng.random()
        if roll >= probability:
            attempts.append(
                {"persona_id": pid, "event_type": "no_action", "reason": "probability_miss", "roll": roll}
            )
            continue

        event_type, type_roll = _pick_type(
            rng, config.type_weights, allow_friend_request=not is_friend
        )
        delay = config.delay_min_s
        if config.delay_max_s > config.delay_min_s:
            delay = rng.randint(config.delay_min_s, config.delay_max_s)
        planned.append(
            PlannedEvent(
                persona_id=pid,
                event_type=event_type,
                delay_s=int(delay),
                roll=float(type_roll),
                score=score,
                is_friend=is_friend,
                reason="hit",
            )
        )
        attempts.append(
            {
                "persona_id": pid,
                "event_type": event_type,
                "reason": "planned",
                "roll": float(type_roll),
            }
        )

    if not planned:
        attempts.append({"persona_id": "", "event_type": "no_action", "reason": "no_event", "roll": 0.0})
    return planned, attempts


__all__ = [
    "EVENT_TYPES",
    "PUBLIC_VISIBILITY",
    "PlannedEvent",
    "SocialConfig",
    "extract_tags",
    "match_score",
    "plan_events",
    "rank_candidates",
]
