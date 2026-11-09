import asyncio

from app.v2.db import DB
from app.v3.schema import ensure_schema
from app.voice.catalog import verify_voice, verified_voice


def test_voice_is_ready_only_after_actual_nonempty_synthesis(tmp_path):
    db = DB(str(tmp_path / "verification.db"))
    db.init()
    ensure_schema(db, seed=False)

    class Edge:
        async def synthesize_voice(self, text, voice, **kwargs):
            return []

    assert asyncio.run(verify_voice(db, "edge", "ja-JP-NanamiNeural", edge_engine=Edge())) is False
    assert not verified_voice(db, "edge", "ja-JP-NanamiNeural")

    class WorkingEdge:
        async def synthesize_voice(self, text, voice, **kwargs):
            return [0.1] * 24000

    assert asyncio.run(verify_voice(db, "edge", "ja-JP-NanamiNeural", edge_engine=WorkingEdge())) is True
    assert verified_voice(db, "edge", "ja-JP-NanamiNeural")
    db.close()
