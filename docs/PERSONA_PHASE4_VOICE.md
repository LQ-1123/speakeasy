# 第四阶段（语音专项）：一个角色 = 一个声音

> 用户验收反馈（原文）：
> **「消息中的语音和用户动态中的语音不一样。砍掉换口音。所有口音不应该用 edgetts，而应该用 fish 的大量音色库。并保证 AI 音色统一性，动态里朗读音色和聊天音色应该统一。」**

## 0. 现场诊断（已实测）

| 证据 | 结论 |
|---|---|
| `data/audio` 里 14:07/14:09/14:10 出现 `tts_*.wav` | 聊天走的是 **Edge** 合成 |
| `data/v3_audio/v3` 同名时段是 `*-fish-*.mp3` | 动态朗读走的是 **Fish**（或 Fish 失败→tone） |
| 日志 `v3 Fish TTS 失败（连接 Fish 失败: All connection attempts failed），回落 edge` | Fish 当前**网络不可达** |
| 日志 `Edge 合成失败（voice=fish-d7294566: Invalid voice）` | **bug**：动态朗读把 Fish 音色 id 当 Edge 声线用，必然失败→tone |
| 本机 DNS 给 `api.fish.audio → 157.240.6.35`（Facebook 段），8.8.8.8/114 给出 Cloudflare 段；直连超时 | 本机 DNS 被污染 + 出口被墙，Fish 需要代理/VPN 才能用 |

**根因**：三条链路各自解析音色（聊天读 `friends.fish_voice_id`、朗读读 `personas.fish_voice_id`、降级用 `voice_id`/`fish-xxx`），
Fish 一失败就各自回落到不同声线 → 同一个人两个声音。

## 1. 目标（可验收）

1. **同一个角色在任何链路都解析出同一个声音**：聊天 / 动态朗读 / 语音通话 / 试听，四条路径共用 **一个** resolver。
2. **正常态 = Fish**：每个角色一个固定 Fish 音色（来自 Fish 音色库，按语言+性别匹配，尽量一人一色）。
3. **降级态 = 同一声线**：Fish 不可达时，四条路径统统降级到**该角色自己的 Edge 声线**，绝不出现「聊天是 A、朗读是 B」。
4. **砍掉「换口音」**：删除换口音入口、口音市场（`AccentMarketDialog`）以及 AI 角色按"口音"选声线的逻辑。
5. **缓存不串味**：音频缓存文件名带 provider（`fish-…` / `edge-…`），Fish 恢复后自动重新合成 Fish 版本，不会继续发旧降级音频。
6. **可观测**：`/api/v3/health` 与 `/api/v3/stats/persona` 暴露 `voice` 段（provider、fish 可达、多少人有 fish 音色、最近失败原因）。

## 2. 文件归属

| 所有者 | 文件 |
|---|---|
| **Lead** | `backend/app/voice/voice_registry.py`（新增）、`backend/app/v3/audio_cache.py`、`backend/app/v2/chat_service.py`（音色解析部分）、`backend/app/v2/router.py`（`/tts/sample`）、`backend/app/v3/service.py`、`backend/app/v3/router.py`、`backend/tests/test_voice_unification.py`、docs、前端构建 |
| **speech-dna** | `backend/app/v3/schema.py` ❌ 不动；`backend/app/v3/seed_data.py`、`backend/app/voice/accent_market.py`（下线）、`backend/app/custom_store.py`、`backend/app/market_api.py`、`backend/tests/test_v3_speech_dna.py` 里音色相关用例 |
| **event-content** | 前端 `frontend/src/components/v2/ChatView.tsx`、`frontend/src/components/AccentMarketDialog.tsx`、`frontend/src/app/page.tsx`、`frontend/src/app/classic/page.tsx`、`frontend/src/components/CreatePersonaDialog.tsx`、`frontend/src/w4/**`（若含口音入口） |

## 3. 统一接口（Lead 实施）

```python
# backend/app/voice/voice_registry.py
@dataclass(frozen=True)
class VoiceBinding:
    persona_id: str
    provider: str      # "fish" | "edge" | "tone"
    ref: str           # fish_voice_id（provider=fish 时有效）
    edge_voice: str    # 降级用（永远是一个合法 Edge 声线名）
    token: str         # 缓存键：fish-<hash8> / edge-<voice>

def binding_for_persona(persona: dict, cfg: Any) -> VoiceBinding
def binding_for_friend(db, friend: dict) -> VoiceBinding      # 按 persona_id 实时查 persona，避免好友行陈旧
def valid_edge_voice(persona: dict) -> str                    # persona.voice_id 合法则用，否则按语言+性别挑
async def synthesize_for_persona(db, persona, text, *, emotion="neutral",
                                 audio_format="wav", cfg=None, edge_engine=None) -> tuple[bytes, str, str]
    """返回 (音频字节, 实际 provider, token)。降级顺序 fish → edge(角色自己的声线) → tone。
    任何链路都必须走这里，禁止再自己拼 reference_id。"""
```

**规则**：动态朗读**不再按帖子语言换声线**——永远用角色自己的声音（Fish 多语言；Edge 降级也用同一角色声线）。

## 4. 验收

1. `pytest backend/tests/test_voice_unification.py` 全绿。
2. 同一个人：`GET /api/v3/persona-posts/{id}/speech` 的 `X-Voice` 头 == 聊天消息音频的 voice token（同一 provider + 同一 ref）。
3. 断网（Fish 不可达）时：四条路径都产出 `edge-<persona.voice_id>` 的音频，且**互相一致**；日志明确写"Fish 不可达，统一降级"。
4. 前端：全局搜索不到「换口音 / 口音市场」入口。
5. 文档补充 `docs/PERSONA_ACCEPTANCE.md` §10。
