"""V3 行为选择引擎（纯逻辑）单元测试。

``app/v3/behavior.py`` 不碰 DB / LLM / 网络，只依赖注入的 ``rng``，因此这里可以
**完全确定性**地验证契约：情境识别、DNA → 行为权重、intent/mood/history 修正、
归一化、抽样分布（500 次固定 seed）、以及给 LLM 的本轮指令。

分布类断言全部用 ``random.Random(42)`` 跑 500 次；因为实现是纯确定性加权抽样，
同一 seed 结果恒定，阈值仍刻意留了余量（实测值见各自注释），避免后续调参时误伤。
"""

from __future__ import annotations

import random
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v3 import behavior as B  # noqa: E402

# 抽样次数（契约要求 500 次）
N = 500
SEED = 42


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _traits(**overrides) -> dict[str, float]:
    """中性 DNA（全 0.5），按需覆盖某些维度。"""
    base = {key: 0.5 for key in B.TRAIT_KEYS}
    base.update(overrides)
    return base


def _samples(inp: B.BehaviorInput, n: int = N, seed: int = SEED) -> list[str]:
    """用同一个 rng 连续抽 n 次（可复现）。"""
    rng = random.Random(seed)
    return [B.choose_behavior(inp, rng=rng).behavior for _ in range(n)]


def _share(seq: list[str], names: tuple[str, ...] | set[str]) -> float:
    wanted = set(names)
    return sum(1 for item in seq if item in wanted) / len(seq)


def _input(user_text: str = "I made pasta for dinner.", **kwargs) -> B.BehaviorInput:
    kwargs.setdefault("dna_traits", _traits())
    return B.BehaviorInput(user_text=user_text, **kwargs)


# --------------------------------------------------------------------------- #
# 契约常量与数据结构
# --------------------------------------------------------------------------- #
def test_contract_constants():
    assert B.BEHAVIORS == (
        "respond", "share", "joke", "tease", "empathize", "comfort", "disagree",
        "complain", "celebrate", "tell_story", "change_topic", "acknowledge",
        "end_conversation", "ask_question",
    )
    assert len(B.BEHAVIORS) == 14
    assert len(set(B.BEHAVIORS)) == 14

    assert B.INTENTS == (
        "greeting", "question", "good_news", "bad_news", "opinion_probe",
        "small_talk", "closing", "silence", "unknown",
    )
    assert len(B.INTENTS) == 9
    assert len(set(B.INTENTS)) == 9

    # DNA 键固定（behavior 只依赖这 10 个数值）
    assert B.TRAIT_KEYS == (
        "initiative", "warmth", "humour", "directness", "verbosity",
        "disclosure", "emoji", "slang", "playfulness", "social_distance",
    )


def test_behavior_input_defaults_and_frozen():
    inp = B.BehaviorInput(dna_traits=_traits())
    assert inp.user_text == ""
    assert inp.recent_behaviors == ()
    assert inp.closeness == 1
    assert inp.mood is None
    assert inp.turn_index == 0
    assert inp.is_first_turn is False
    assert inp.has_due_open_loop is False
    assert inp.current_activity == ""
    assert inp.time_period == ""
    assert inp.disagreement_style == ""

    with pytest.raises(FrozenInstanceError):
        inp.user_text = "nope"  # type: ignore[misc]


def test_normalize_traits_safe_defaults():
    # 缺键 / 空 / None / 脏值 → 10 键齐全、全部 0..1
    for raw in (None, {}, {"initiative": "not-a-number"}, {"humour": 9}, {"warmth": -3}):
        traits = B.normalize_traits(raw)
        assert set(traits) == set(B.TRAIT_KEYS)
        assert all(0.0 <= value <= 1.0 for value in traits.values())

    assert B.normalize_traits({})["initiative"] == 0.5
    assert B.normalize_traits({"humour": 9})["humour"] == 1.0
    assert B.normalize_traits({"warmth": -3})["warmth"] == 0.0
    assert B.normalize_traits({"initiative": "bad"})["initiative"] == 0.5
    # 不修改入参
    payload = {"humour": 0.9}
    B.normalize_traits(payload)
    assert payload == {"humour": 0.9}


# --------------------------------------------------------------------------- #
# detect_intent
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, expected",
    [
        # greeting
        ("hey!", "greeting"),
        ("Hello there", "greeting"),
        ("How are you?", "greeting"),
        ("你好", "greeting"),
        ("在吗", "greeting"),
        # question
        ("Do you like jazz?", "question"),
        ("Where are you from?", "question"),
        ("你睡了吗", "question"),
        ("你周末一般干嘛呢", "question"),
        # good_news
        ("I got the job!!", "good_news"),
        ("我升职了！", "good_news"),
        # bad_news
        ("My cat died yesterday.", "bad_news"),
        ("我的猫去世了", "bad_news"),
        ("I broke up with her", "bad_news"),
        # opinion_probe
        ("What do you think I should do?", "opinion_probe"),
        ("你觉得我该去吗", "opinion_probe"),
        ("Should I text him?", "opinion_probe"),
        # small_talk
        ("I had a long day at work.", "small_talk"),
        ("今天好累啊", "small_talk"),
        ("thanks", "small_talk"),
        # closing
        ("good night, talk tomorrow", "closing"),
        ("晚安，明天聊", "closing"),
        ("see you later!", "closing"),
        # silence
        ("", "silence"),
        ("   ", "silence"),
        ("...", "silence"),
        ("嗯", "silence"),
        ("ok", "silence"),
        # unknown
        ("asdf", "unknown"),
        ("xqzw", "unknown"),
    ],
)
def test_detect_intent(text, expected):
    assert B.detect_intent(text) == expected


def test_detect_intent_precedence():
    # 坏消息优先于问候：不能在别人说坏消息时回 "hey!"
    assert B.detect_intent("Hey, my dog died yesterday.") == "bad_news"
    # 征求意见优先于普通疑问
    assert B.detect_intent("Should I move to Berlin? What do you think?") == "opinion_probe"
    # 疑问优先于告别（"你睡了吗" 不是 "晚安"）
    assert B.detect_intent("你睡了吗") == "question"
    assert B.detect_intent("晚安，明天聊") == "closing"
    # 纯附和 / 语气词是 silence，不是 unknown
    assert B.detect_intent("好的") == "silence"
    assert B.detect_intent("hmm") == "silence"


def test_detect_intent_edge_inputs():
    assert B.detect_intent(None) == "silence"  # type: ignore[arg-type]
    assert B.detect_intent("😭😭") == "silence"  # 纯 emoji，无文字内容
    assert B.detect_intent("  hello  ") == "greeting"  # 前后空白
    assert B.detect_intent("HELLO") == "greeting"  # 大小写
    # 任何输入都必须落在 INTENTS 内
    for text in ("", "?", "what", "你好呀", "I am 25", "lol ok"):
        assert B.detect_intent(text) in B.INTENTS


# --------------------------------------------------------------------------- #
# 权重归一化 / 抽样基础
# --------------------------------------------------------------------------- #
_CASES = [
    _input(),
    _input(user_text="hey!"),
    _input(user_text="My cat died yesterday."),
    _input(user_text="good night"),
    _input(dna_traits=_traits(initiative=0.0, warmth=0.0, humour=0.0, directness=0.0,
                              verbosity=1.0, disclosure=0.0, emoji=0.0, slang=0.0,
                              playfulness=0.0, social_distance=1.0)),
    _input(dna_traits=_traits(initiative=1.0, warmth=1.0, humour=1.0, directness=1.0,
                              verbosity=0.0, disclosure=1.0, emoji=1.0, slang=1.0,
                              playfulness=1.0, social_distance=0.0)),
    _input(mood={"energy": 0.0, "stress": 1.0, "social_battery": 0.0}),
    _input(closeness=5, has_due_open_loop=True, is_first_turn=True, turn_index=20),
]


@pytest.mark.parametrize("inp", _CASES)
def test_weights_normalized_and_respond_positive(inp):
    choice = B.choose_behavior(inp, rng=random.Random(SEED))
    assert choice.behavior in B.BEHAVIORS
    assert set(choice.weights) == set(B.BEHAVIORS)
    assert sum(choice.weights.values()) == pytest.approx(1.0, abs=1e-9)
    assert all(value >= 0.0 for value in choice.weights.values())
    # respond 永远保留下限 → 归一化后必 > 0
    assert choice.weights["respond"] > 0.0
    # confidence = 选中行为的归一化权重
    assert choice.confidence == pytest.approx(choice.weights[choice.behavior], abs=1e-6)
    assert 0.0 < choice.confidence <= 1.0
    assert choice.reason.startswith("intent=")


def test_choose_behavior_reproducible_with_seed():
    inp = _input(user_text="I had a long day at work.", closeness=3)
    rng_a = random.Random(SEED)
    rng_b = random.Random(SEED)
    seq_a = [B.choose_behavior(inp, rng=rng_a).behavior for _ in range(100)]
    seq_b = [B.choose_behavior(inp, rng=rng_b).behavior for _ in range(100)]
    assert seq_a == seq_b

    # 不同 seed 允许不同（只要求同 seed 可复现）
    seq_c = [B.choose_behavior(inp, rng=random.Random(7)).behavior for _ in range(100)]
    assert seq_a != seq_c


def test_rng_none_still_works():
    choice = B.choose_behavior(_input(), rng=None)
    assert choice.behavior in B.BEHAVIORS
    assert sum(choice.weights.values()) == pytest.approx(1.0, abs=1e-9)

    # 也接受"看起来像"的非法 rng（安全回落）
    choice2 = B.choose_behavior(_input(), rng=object())  # type: ignore[arg-type]
    assert choice2.behavior in B.BEHAVIORS


def test_all_sampled_behaviors_are_known():
    for inp in _CASES:
        for behavior in _samples(inp, n=200):
            assert behavior in B.BEHAVIORS


# --------------------------------------------------------------------------- #
# DNA → 行为（倍数关系）
# --------------------------------------------------------------------------- #
def test_high_initiative_asks_more():
    """契约：高 initiative 的 ask_question 占比 ≥ 低 initiative 的 2 倍。

    实测（seed=42, n=500）：0.014 vs 0.168，约 12 倍。
    """
    low = _samples(_input(dna_traits=_traits(initiative=0.10)))
    high = _samples(_input(dna_traits=_traits(initiative=0.90)))
    low_ask = _share(low, ("ask_question",))
    high_ask = _share(high, ("ask_question",))
    assert high_ask > 0
    assert high_ask >= 2.0 * low_ask
    # 同时给出方向性断言，避免两边都塌成 0
    assert high_ask >= 0.08
    assert low_ask <= 0.05


def test_ask_question_capped_by_initiative():
    """ask_question 的上限受 initiative 约束，不会靠 context 堆成必问。"""
    low_inp = _input(
        dna_traits=_traits(initiative=0.05),
        closeness=1,
        has_due_open_loop=True,
        mood={"energy": 1.0, "stress": 0.0, "social_battery": 1.0},
    )
    high_inp = _input(
        dna_traits=_traits(initiative=1.0),
        closeness=1,
        has_due_open_loop=True,
        mood={"energy": 1.0, "stress": 0.0, "social_battery": 1.0},
    )
    low_weight = B.choose_behavior(low_inp, rng=random.Random(SEED)).weights["ask_question"]
    high_weight = B.choose_behavior(high_inp, rng=random.Random(SEED)).weights["ask_question"]
    assert low_weight < 0.06
    assert high_weight >= 3.0 * low_weight
    # 即便 initiative=1 且 open loop 加持，也不能变成"每轮都问"
    assert high_weight < 0.45


def test_humour_raises_joke_and_tease():
    """契约：humour 高 → joke+tease 占比 ≥ 1.5 倍。

    实测（seed=42, n=500）：0.008 vs 0.106，约 13 倍。
    """
    low = _samples(_input(dna_traits=_traits(humour=0.10, playfulness=0.5)))
    high = _samples(_input(dna_traits=_traits(humour=0.90, playfulness=0.5)))
    low_fun = _share(low, ("joke", "tease"))
    high_fun = _share(high, ("joke", "tease"))
    assert high_fun > 0
    assert high_fun >= 1.5 * low_fun
    assert high_fun >= 0.05


def test_playfulness_raises_tease():
    # playfulness 单独也是 tease 的正相关（tease 要求 humour × playfulness）
    flat = B.choose_behavior(
        _input(dna_traits=_traits(humour=0.8, playfulness=0.1), closeness=5),
        rng=random.Random(SEED),
    ).weights
    playful = B.choose_behavior(
        _input(dna_traits=_traits(humour=0.8, playfulness=0.95), closeness=5),
        rng=random.Random(SEED),
    ).weights
    assert playful["tease"] > flat["tease"] * 1.5


def test_disclosure_raises_share_and_story():
    flat = B.choose_behavior(
        _input(dna_traits=_traits(disclosure=0.1, verbosity=0.5), closeness=2),
        rng=random.Random(SEED),
    ).weights
    open_ = B.choose_behavior(
        _input(dna_traits=_traits(disclosure=0.95, verbosity=0.5), closeness=2),
        rng=random.Random(SEED),
    ).weights
    assert open_["share"] > flat["share"] * 2.0
    assert open_["tell_story"] > flat["tell_story"] * 2.0


def test_warmth_raises_empathize_and_comfort():
    cold = B.choose_behavior(
        _input(dna_traits=_traits(warmth=0.05)), rng=random.Random(SEED)
    ).weights
    warm = B.choose_behavior(
        _input(dna_traits=_traits(warmth=0.95)), rng=random.Random(SEED)
    ).weights
    assert warm["empathize"] > cold["empathize"] * 3.0
    assert warm["comfort"] > cold["comfort"] * 3.0


def test_directness_raises_disagree_and_avoid_style_dampens():
    soft = B.choose_behavior(
        _input(dna_traits=_traits(directness=0.1)), rng=random.Random(SEED)
    ).weights
    blunt = B.choose_behavior(
        _input(dna_traits=_traits(directness=0.95)), rng=random.Random(SEED)
    ).weights
    assert blunt["disagree"] > soft["disagree"] * 2.0

    avoid = B.choose_behavior(
        _input(dna_traits=_traits(directness=0.95), disagreement_style="avoid"),
        rng=random.Random(SEED),
    ).weights
    # disagreement_style="avoid" → disagree 权重≈0.02（远低于默认）
    assert avoid["disagree"] < 0.3 * blunt["disagree"]


def test_social_distance_direction_is_closeness():
    """方向约定：``social_distance`` 1.0 = 亲近（与 speech_dna.dna_traits 一致）。

    这条测试锁住集成方向：如果哪天把 1.0 当成"疏远"，亲密人物会突然变冷淡。
    """
    distant = B.choose_behavior(
        _input(dna_traits=_traits(social_distance=0.1), closeness=3),
        rng=random.Random(SEED),
    ).weights
    intimate = B.choose_behavior(
        _input(dna_traits=_traits(social_distance=0.95), closeness=3),
        rng=random.Random(SEED),
    ).weights

    # 越亲近越愿意分享自己 / 讲故事 / 逗对方；越疏远越容易换话题、只做短回应
    assert intimate["share"] > distant["share"]
    assert intimate["tell_story"] > distant["tell_story"]
    assert intimate["tease"] > distant["tease"]
    assert distant["change_topic"] > intimate["change_topic"]
    assert distant["acknowledge"] > intimate["acknowledge"]


def test_low_verbosity_raises_acknowledge():
    terse = B.choose_behavior(
        _input(dna_traits=_traits(verbosity=0.05)), rng=random.Random(SEED)
    ).weights
    wordy = B.choose_behavior(
        _input(dna_traits=_traits(verbosity=0.95)), rng=random.Random(SEED)
    ).weights
    assert terse["acknowledge"] > wordy["acknowledge"] * 2.0


# --------------------------------------------------------------------------- #
# intent / mood / history
# --------------------------------------------------------------------------- #
def test_bad_news_empathy_dominates():
    """契约：bad_news 下 empathize+comfort+respond 合计 > 60%。

    实测（seed=42, n=500）：中性人物 0.780；低温暖高幽默（对抗性人设）0.690。
    """
    cases = [
        _input(user_text="My cat died yesterday."),
        _input(user_text="我的猫去世了"),
        # 对抗性人设：低温暖 + 高幽默 + 高直接度 + 疏远，仍然不许讲段子
        _input(
            dna_traits=_traits(warmth=0.10, humour=0.90, directness=0.90,
                               initiative=0.90, social_distance=0.15),
            user_text="My cat died yesterday.",
        ),
    ]
    for inp in cases:
        seq = _samples(inp)
        empathy = _share(seq, ("empathize", "comfort", "respond"))
        assert empathy > 0.60, empathy
        # 坏消息下 joke/tease/celebrate 被强力压低
        assert _share(seq, ("joke", "tease", "celebrate")) < 0.06


def test_good_news_celebrates():
    seq = _samples(_input(user_text="I got the job!!"))
    celebrate = _share(seq, ("celebrate",))
    assert celebrate >= 0.25
    # 好消息下 celebrate 明显高于普通闲聊基线（基线约 0.06）
    baseline = _share(_samples(_input(user_text="I made pasta for dinner.")), ("celebrate",))
    assert celebrate >= 3.0 * baseline
    # 好消息下不会突然开始抱怨 / 讲段子
    assert _share(seq, ("complain",)) < 0.10


def test_closing_raises_end_and_acknowledge():
    """契约：closing 下 end_conversation + acknowledge 明显上升。

    实测（seed=42, n=500）：0.518 vs 基线 0.110（约 4.7 倍）。
    """
    closing = _samples(_input(user_text="good night, talk tomorrow"))
    closing_zh = _samples(_input(user_text="晚安，明天聊"))
    baseline = _samples(_input(user_text="I made pasta for dinner."))

    close_share = _share(closing, ("end_conversation", "acknowledge"))
    close_zh_share = _share(closing_zh, ("end_conversation", "acknowledge"))
    base_share = _share(baseline, ("end_conversation", "acknowledge"))

    assert close_share >= 2.5 * base_share
    assert close_share >= 0.35
    assert close_zh_share >= 0.35
    # 收尾时基本不追问
    assert _share(closing, ("ask_question",)) < 0.05


def test_consecutive_ask_question_suppressed():
    """契约：连续两次 ask_question 之后，第三次 < 5%。

    实测（seed=42, n=500）：0.006。
    """
    inp = _input(
        dna_traits=_traits(initiative=0.95),
        user_text="Tell me about your day?",
        recent_behaviors=("ask_question", "ask_question"),
    )
    seq = _samples(inp)
    third_ask = _share(seq, ("ask_question",))
    assert third_ask < 0.05
    assert third_ask >= 0.0

    # 只问过一次也已经强压（最近 2 轮出现 → ×0.15）
    once = _samples(
        _input(
            dna_traits=_traits(initiative=0.95),
            user_text="Tell me about your day?",
            recent_behaviors=("ask_question",),
        )
    )
    assert _share(once, ("ask_question",)) < 0.08


def test_low_energy_high_stress_shifts_behavior():
    """契约：energy 低 / stress 高 → complain/acknowledge 上升、celebrate 下降。

    实测（seed=42, n=500）：complain+ack 0.176 → 0.338（1.9 倍）；
    celebrate 0.054 → 0.004（0.07 倍）。
    """
    tired = _samples(
        _input(mood={"energy": 0.20, "stress": 0.80, "social_battery": 0.50})
    )
    normal = _samples(_input(mood={"energy": 0.60, "stress": 0.35, "social_battery": 0.60}))

    tired_down = _share(tired, ("complain", "acknowledge"))
    normal_down = _share(normal, ("complain", "acknowledge"))
    assert tired_down >= 1.4 * normal_down

    tired_celebrate = _share(tired, ("celebrate",))
    normal_celebrate = _share(normal, ("celebrate",))
    assert tired_celebrate <= 0.4 * normal_celebrate

    # 低体力时更容易收尾
    low_battery = _samples(_input(mood={"energy": 0.5, "stress": 0.3, "social_battery": 0.15}))
    assert _share(low_battery, ("end_conversation",)) > _share(
        normal, ("end_conversation",)
    )


def test_dirty_mood_is_safe():
    for mood in (None, {}, {"energy": "x"}, {"stress": None}, {"social_battery": 5}, "oops"):
        inp = _input(mood=mood)  # type: ignore[arg-type]
        choice = B.choose_behavior(inp, rng=random.Random(SEED))
        assert choice.behavior in B.BEHAVIORS
        assert sum(choice.weights.values()) == pytest.approx(1.0, abs=1e-9)


def test_end_conversation_low_but_nonzero_outside_gate():
    # 非收尾情境：end_conversation 很低，但绝不是 0（"允许结束"）
    weights = B.choose_behavior(
        _input(user_text="I made pasta for dinner."), rng=random.Random(SEED)
    ).weights
    assert 0.0 < weights["end_conversation"] < 0.02
    # 收尾情境被放大（闸门生效）
    closing_weights = B.choose_behavior(
        _input(user_text="good night, talk tomorrow"), rng=random.Random(SEED)
    ).weights
    assert closing_weights["end_conversation"] > weights["end_conversation"] * 10.0


def test_open_loop_and_first_turn():
    plain = B.choose_behavior(_input(turn_index=5), rng=random.Random(SEED)).weights
    loop = B.choose_behavior(
        _input(turn_index=5, has_due_open_loop=True), rng=random.Random(SEED)
    ).weights
    assert loop["ask_question"] > plain["ask_question"] * 2.0

    first = B.choose_behavior(_input(is_first_turn=True), rng=random.Random(SEED)).weights
    later = B.choose_behavior(_input(is_first_turn=False), rng=random.Random(SEED)).weights
    assert first["respond"] > later["respond"]
    assert first["end_conversation"] < later["end_conversation"]


def test_time_period_late_night_suppresses_questions():
    night = B.choose_behavior(
        _input(time_period="late_night"), rng=random.Random(SEED)
    ).weights
    day = B.choose_behavior(_input(time_period="afternoon"), rng=random.Random(SEED)).weights
    assert night["ask_question"] < day["ask_question"]
    assert night["end_conversation"] > day["end_conversation"]


# --------------------------------------------------------------------------- #
# behavior_history_penalty
# --------------------------------------------------------------------------- #
def test_behavior_history_penalty_formula():
    assert B.behavior_history_penalty((), "share") == 1.0
    assert B.behavior_history_penalty([], "share") == 1.0
    # 最近一轮出现 → ×0.35
    assert B.behavior_history_penalty(("share",), "share") == pytest.approx(0.35)
    # 两连 → 0.35 × (1 - 0.65*0.35)
    expected_two = 0.35 * (1.0 - 0.65 * 0.35)
    assert B.behavior_history_penalty(("share", "share"), "share") == pytest.approx(expected_two)
    # 越久远惩罚越轻（最新最重）：最新一轮 = 0.35，第二轮 = 衰减后的 0.7725
    newest = B.behavior_history_penalty(("respond", "share"), "share")
    older = B.behavior_history_penalty(("share", "respond"), "share")
    assert newest == pytest.approx(0.35)
    assert older == pytest.approx(1.0 - 0.65 * 0.35)
    assert newest < older < 1.0


def test_behavior_history_penalty_ask_question_extra():
    single = B.behavior_history_penalty(("ask_question",), "ask_question")
    assert single == pytest.approx(0.35 * B.ASK_QUESTION_EXTRA)
    assert single < 0.06
    # 连续两次 → 近乎归零
    double = B.behavior_history_penalty(("ask_question", "ask_question"), "ask_question")
    assert double < 0.05
    # 其它行为只吃基础惩罚，没有 ×0.15
    assert B.behavior_history_penalty(("ask_question",), "respond") == 1.0


def test_behavior_history_penalty_window_and_filters():
    # 只保留最近 3 轮：更早的 share 不计入
    assert B.behavior_history_penalty(
        ("share", "joke", "respond", "share"), "share"
    ) == pytest.approx(0.35)
    # 未知行为名 → 1.0（不惩罚）
    assert B.behavior_history_penalty(("share",), "not_a_behavior") == 1.0
    # 非字符串 / 未知名称被过滤，不影响已知行为
    assert B.behavior_history_penalty(
        [None, 123, "share"], "share"  # type: ignore[list-item]
    ) == pytest.approx(0.35)
    # 惩罚永远在 0..1
    for recent, name in [
        (("share", "share", "share"), "share"),
        (("ask_question", "ask_question", "ask_question"), "ask_question"),
    ]:
        value = B.behavior_history_penalty(recent, name)
        assert 0.0 <= value <= 1.0


# --------------------------------------------------------------------------- #
# behavior_directive
# --------------------------------------------------------------------------- #
def _choice(behavior: str) -> B.BehaviorChoice:
    return B.BehaviorChoice(
        behavior=behavior,
        reason="test",
        confidence=0.25,
        weights={name: 1.0 / len(B.BEHAVIORS) for name in B.BEHAVIORS},
    )


def test_directive_question_rule():
    """只有 ask_question 允许以问题结尾；其它行为必须写死禁止提问。"""
    for behavior in B.BEHAVIORS:
        directive = B.behavior_directive(
            _choice(behavior), dna_traits=_traits(), language="en"
        )
        assert behavior in directive
        if behavior == "ask_question":
            assert "question" in directive
            assert "Do not end with a question" not in directive
        else:
            assert "Do not end with a question this turn unless they asked you one." in directive
        assert len(directive) < 1200


def test_directive_length_follows_verbosity():
    terse = B.behavior_directive(
        _choice("share"), dna_traits=_traits(verbosity=0.05), language="en"
    )
    wordy = B.behavior_directive(
        _choice("share"), dna_traits=_traits(verbosity=0.95), language="en"
    )
    assert "no more than 8 words" in terse
    assert "3-4 sentences is fine" in wordy
    assert terse != wordy


def test_directive_acknowledge_and_end_conversation():
    ack = B.behavior_directive(
        _choice("acknowledge"), dna_traits=_traits(verbosity=0.95), language="en"
    )
    assert "one or two words can be perfect" in ack
    assert "Do not end with a question" in ack

    end = B.behavior_directive(
        _choice("end_conversation"), dna_traits=_traits(verbosity=0.5), language="en"
    )
    assert "sign-off" in end
    assert "do not ask a question" in end.lower()
    assert "Do not end with a question" in end


def test_directive_language_and_unknown_behavior():
    assert "Write the reply in English." in B.behavior_directive(
        _choice("respond"), dna_traits=_traits(), language="en"
    )
    assert "Write the reply in Chinese" in B.behavior_directive(
        _choice("respond"), dna_traits=_traits(), language="zh"
    )
    assert "Write the reply in fr." in B.behavior_directive(
        _choice("respond"), dna_traits=_traits(), language="fr"
    )
    # 未知行为名安全回落成 respond 指令
    fallback = B.behavior_directive(
        _choice("bogus"), dna_traits=_traits(), language="en"
    )
    assert "respond" in fallback
    assert "Do not end with a question" in fallback
