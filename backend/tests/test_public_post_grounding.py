"""Publishing keeps only reviewed text; model failure is not approval."""
import json

import pytest

from app.v3 import life_posts
from app.v3.social_writing import grounded_post


class ReviewModel:
    uses_mock = False

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def complete_json(self, messages, **kwargs):
        self.calls.append(messages)
        return json.dumps(self.result)


async def test_factual_edit_replaces_unsupported_details_without_new_generation():
    llm = ReviewModel({"content": "Gave up on the book. Kept the bookmark."})
    text = await grounded_post(llm, "Gave up after nine pages. My cat ate the bookmark.",
                               source={"event": "Gave up halfway; kept the bookmark"}, language="en")
    assert text == "Gave up on the book. Kept the bookmark."
    assert len(llm.calls) == 1
    data = json.loads(llm.calls[0][1]["content"])
    assert data["source"]["event"] == "Gave up halfway; kept the bookmark"


@pytest.mark.parametrize("result", [{}, [], {"content": ""}, {"content": 42}, {"content": "x" * 401},
                                     {"content": "完全错误的语言"}])
async def test_invalid_or_failed_review_never_returns_unreviewed_draft(result):
    assert await grounded_post(ReviewModel(result), "My cat ate the bookmark.", source={}, language="en") == ""


async def test_caption_uses_same_review_and_metadata_only_alt():
    class Model(ReviewModel):
        async def complete_json(self, messages, **kwargs):
            self.calls.append(messages)
            if messages[0]["content"].startswith("Fact editor"):
                return json.dumps({"content": "Someone else's notes in a used book. I'd read the margins."})
            return json.dumps({"content": "Notes about nine rainy days.", "alt": "A cat on a red table"})
    llm = Model({})
    result = await life_posts.image_caption(llm, {"language": "en", "nickname": "Mei"},
                                           title="Used book with handwritten notes", tags=["books"], recent=[])
    assert "rainy" not in result["content"]
    assert result["alt"] == "Used book with handwritten notes"
    assert len(llm.calls) == 2
