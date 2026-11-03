"""流式断句器测试：缩写/小数/省略号/引号/首句激进/max_chars 兜底/逐字符一致性。"""

from __future__ import annotations

import pytest

from app.llm.sentence_splitter import StreamingSentenceSplitter


def split_stream(text: str, **kw) -> list[str]:
    """逐字符喂入（模拟真实 token 流），返回所有句子。"""
    sp = StreamingSentenceSplitter(**kw)
    out: list[str] = []
    for ch in text:
        out.extend(sp.feed(ch))
    out.extend(sp.flush())
    return out


def split_bulk(text: str, **kw) -> list[str]:
    sp = StreamingSentenceSplitter(**kw)
    out = list(sp.feed(text))
    out.extend(sp.flush())
    return out


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #
def test_simple_three_sentences():
    text = "Right then. That's a flat white. Cheers!"
    got = split_stream(text)
    assert len(got) == 3, got
    assert got[0].startswith("Right then")
    assert "flat white" in got[1]
    assert "Cheers" in got[2]


def test_empty_input():
    assert split_stream("") == []


def test_no_terminator_flushed_at_end():
    got = split_stream("just some words with no finish")
    assert len(got) == 1
    assert "just some words" in got[0]


def test_whitespace_only():
    assert split_stream("   \n\t  ") == []


# --------------------------------------------------------------------------- #
# 缩写不切
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,must_contain",
    [
        ("Mr. Smith is here.", "Mr. Smith"),
        ("I saw Dr. Patel yesterday.", "Dr. Patel"),
        ("Use e.g. this one.", "e.g. this"),
        ("That is i.e. the same.", "i.e. the same"),
        ("Apples, oranges, etc. are fruit.", "etc."),
        ("He left at 5 p.m. sharp.", "5 p.m."),
        ("The U.S. team won.", "U.S. team"),
        ("Ms. Jones called.", "Ms. Jones"),
        ("Prof. Lee agreed.", "Prof. Lee"),
    ],
)
def test_abbreviations_not_split(text, must_contain):
    got = split_stream(text)
    joined = " ".join(got)
    assert must_contain in joined, f"{text!r} -> {got}"
    # 缩写处不能被单独切成一句
    for s in got:
        assert s.strip() not in ("Mr.", "Dr.", "e.g.", "i.e.", "etc.", "p.m.", "U.S.", "Ms.", "Prof."), got


# --------------------------------------------------------------------------- #
# 小数 / 版本号
# --------------------------------------------------------------------------- #
def test_decimal_not_split():
    got = split_stream("It costs 3.5 pounds today.")
    assert len(got) == 1, got
    assert "3.5" in got[0]


def test_version_number_not_split():
    got = split_stream("We shipped v1.2.3 last week.")
    assert len(got) == 1, got
    assert "v1.2.3" in got[0]


def test_time_not_split():
    got = split_stream("The meeting is at 10.30 tomorrow.")
    assert len(got) == 1, got
    assert "10.30" in got[0]


# --------------------------------------------------------------------------- #
# 省略号
# --------------------------------------------------------------------------- #
def test_ellipsis_is_one_unit():
    got = split_stream("Well... I'm not sure. Let me think.")
    assert len(got) >= 2, got
    # 不允许出现 ".." 碎片
    for s in got:
        assert s.strip() not in ("..", ".", "…"), got


def test_ellipsis_does_not_break_into_fragments():
    got = split_stream("Right then... a flat white it is.")
    joined = " ".join(got)
    assert "Right then" in joined
    assert "flat white" in joined


# --------------------------------------------------------------------------- #
# 引号 / 括号
# --------------------------------------------------------------------------- #
def test_quote_included_with_sentence():
    got = split_stream('He said "I am fine." Then he left.')
    assert len(got) >= 2, got
    assert '"' in got[0] or "fine" in got[0]
    # 不应该把右引号单独切出去
    assert not any(s.strip() in ('"', '."', "'") for s in got), got


def test_parenthesis_included():
    got = split_stream("He agreed (reluctantly). We moved on.")
    assert len(got) >= 2, got
    assert ")" in got[0] or "reluctantly" in got[0]


# --------------------------------------------------------------------------- #
# 首句激进
# --------------------------------------------------------------------------- #
def test_first_sentence_is_aggressive():
    """首句应在第一个逗号处就切出来，压低首包延迟。"""
    text = (
        "Right then, that's a flat white for you, and a pastry on the side. "
        "Cheers, guv."
    )
    got = split_stream(text, first_chunk_max_chars=40)
    assert len(got) >= 2, got
    assert len(got[0]) < len(text), got
    # 首句应该明显短于整段
    assert len(got[0]) <= 60, f"首句 {len(got[0])} 字符，太长了: {got[0]!r}"


def test_first_sentence_aggressive_can_cut_at_comma():
    got = split_stream("Well, I was going to say something long here.", first_chunk_max_chars=20)
    assert got, "should produce at least one sentence"
    assert len(got[0]) <= 40, f"{got[0]!r}"


# --------------------------------------------------------------------------- #
# max_chars 兜底
# --------------------------------------------------------------------------- #
def test_max_chars_forces_split():
    long_text = " ".join(["word"] * 200) + "."
    got = split_stream(long_text, max_chars=120, first_chunk_max_chars=60)
    assert len(got) > 1, "超长文本应被强切"
    for s in got:
        # 允许少量超出（在空格处切时会有偏移）
        assert len(s) <= 200, f"句子 {len(s)} 字符过长: {s[:80]!r}"


def test_max_chars_prefers_space():
    long_text = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima"
    got = split_stream(long_text + ".", max_chars=50)
    assert len(got) >= 2
    # 切点应该在空格处（不切断单词）
    for s in got[:-1]:
        assert s[-1].isalnum() or s[-1] in ",;:", f"切点异常: {s!r}"


# --------------------------------------------------------------------------- #
# 流式一致性（最重要）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Right then. That's a flat white. Cheers!",
        "Mr. Smith said it costs 3.5 pounds e.g. today... Cheers, guv!",
        'He said "I am fine." Then he left.',
        "One. Two. Three. Four. Five.",
        "No terminator at all",
    ],
)
def test_streaming_equals_bulk(text):
    """逐字符流式喂入与一次性喂入必须产出完全一致的结果。"""
    streamed = split_stream(text, min_chars=2, max_chars=120, first_chunk_max_chars=60)
    bulk = split_bulk(text, min_chars=2, max_chars=120, first_chunk_max_chars=60)
    assert streamed == bulk, f"\nstream={streamed}\nbulk  ={bulk}"


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7, 13])
def test_chunk_size_invariance(chunk_size):
    """任意切块大小的喂入方式结果一致。"""
    text = "Mr. Smith said it costs 3.5 pounds. Cheers, guv! Right then..."
    sp = StreamingSentenceSplitter(min_chars=2, max_chars=120, first_chunk_max_chars=60)
    out: list[str] = []
    for i in range(0, len(text), chunk_size):
        out.extend(sp.feed(text[i : i + chunk_size]))
    out.extend(sp.flush())

    bulk = split_bulk(text, min_chars=2, max_chars=120, first_chunk_max_chars=60)
    assert out == bulk, f"chunk={chunk_size}\nstream={out}\nbulk  ={bulk}"


def test_no_empty_sentences():
    text = "Hi.  ..  Hello!  ?  Bye."
    got = split_stream(text, min_chars=2)
    for s in got:
        assert s.strip(), f"产生了空句子: {got}"


def test_min_chars_respected():
    """短于 min_chars 的碎片不应单独成句。"""
    got = split_stream("A. B. C. This is a real sentence.", min_chars=3)
    assert all(len(s.strip()) >= 3 for s in got), got


def test_flush_idempotent():
    sp = StreamingSentenceSplitter()
    sp.feed("Hello there.")
    first = sp.flush()
    second = sp.flush()
    assert second == [], f"flush 应幂等，第二次得到 {second}"


def test_feed_after_flush_is_safe():
    sp = StreamingSentenceSplitter()
    sp.feed("Hello there")
    sp.flush()
    out = sp.feed("New sentence here.")
    assert isinstance(out, list)
