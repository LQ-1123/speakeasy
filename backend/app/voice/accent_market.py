"""口音市场：全部可用声线 + 元数据（地区、语言、口音来源、示例）。

.. deprecated:: 阶段四（语音专项）
    **口音系统已下线**。用户验收要求「一个角色 = 一个声音」，AI 角色的主音色
    统一由 Fish 音色库按语言+性别自动分配（见 ``app.v3.seed_data.ensure_persona_voices``），
    不再按「口音」挑声线。本模块保留两件事：

    1. 旧数据/旧接口的只读元数据（``get_accent`` 等），避免老库直接报错；
    2. Edge 降级合成时需要的声线元数据。

    新代码**不要**再用它给 AI 角色选声线；``/api/market/accents`` 已标记 deprecated，
    ``/api/market/assign`` 直接返回 410。

设计原则
--------
1. **官方英语变体优先** —— 真实地区口音，100% 自然
2. **外语声线兜底** —— 目标地区没有官方英语变体时（意大利/法国/德国/日本…）
3. 每条声线都带可读的中文/英文标签，前端可直接展示成"市场"

数据来源是 edge-tts 的实际可用声线（已在本机逐条验证过）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 口音系统已下线（阶段四）。保留常量供调用方/测试显式判断。
ACCENT_MARKET_DEPRECATED = True

#: 统一的下线说明（路由响应 / 日志共用）
DEPRECATION_MESSAGE = "口音系统已下线：AI 角色音色统一由 Fish 音色库自动分配。"

# --------------------------------------------------------------------------- #
# 地区元数据
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RegionMeta:
    """一个地区的口音元信息。"""

    locale: str          # en-GB / it-IT ...
    label_zh: str        # 英国
    label_en: str        # United Kingdom
    flag: str            # 🇬🇧
    #: 口音类型：official = 官方英语变体；foreign_fallback = 外语声线读英文
    kind: str = "official"
    #: 这个口音给人的感觉（供 UI 展示）
    vibe: str = ""


#: 官方英语变体地区
OFFICIAL_REGIONS: dict[str, RegionMeta] = {
    "en-GB": RegionMeta("en-GB", "英国", "United Kingdom", "🇬🇧", "official",
                        "标准英音 RP，伦敦中产腔，字正腔圆"),
    "en-IE": RegionMeta("en-IE", "爱尔兰", "Ireland", "🇮🇪", "official",
                        "爱尔兰英语，句尾上扬，有音乐感"),
    "en-AU": RegionMeta("en-AU", "澳大利亚", "Australia", "🇦🇺", "official",
                        "澳洲英语，慵懒松弛，元音偏扁"),
    "en-NZ": RegionMeta("en-NZ", "新西兰", "New Zealand", "🇳🇿", "official",
                        "新西兰英语，短促，元音位置上移"),
    "en-CA": RegionMeta("en-CA", "加拿大", "Canada", "🇨🇦", "official",
                        "加拿大英语，介于美音和英音之间"),
    "en-US": RegionMeta("en-US", "美国", "United States", "🇺🇸", "official",
                        "美式英语，卷舌明显，节奏轻快"),
    "en-IN": RegionMeta("en-IN", "印度", "India", "🇮🇳", "official",
                        "印度英语，音节计时，语调起伏大"),
    "en-SG": RegionMeta("en-SG", "新加坡", "Singapore", "🇸🇬", "official",
                        "新加坡英语，语速快，句尾上扬"),
    "en-ZA": RegionMeta("en-ZA", "南非", "South Africa", "🇿🇦", "official",
                        "南非英语，元音偏前，干脆利落"),
    "en-HK": RegionMeta("en-HK", "香港", "Hong Kong", "🇭🇰", "official",
                        "香港英语，粤语底子，音节清晰"),
    "en-KE": RegionMeta("en-KE", "肯尼亚", "Kenya", "🇰🇪", "official",
                        "肯尼亚英语，音节计时，语调平稳"),
    "en-NG": RegionMeta("en-NG", "尼日利亚", "Nigeria", "🇳🇬", "official",
                        "尼日利亚英语，节奏强，富有张力"),
    "en-PH": RegionMeta("en-PH", "菲律宾", "Philippines", "🇵🇭", "official",
                        "菲律宾英语，美音底子，音节分明"),
    "en-TZ": RegionMeta("en-TZ", "坦桑尼亚", "Tanzania", "🇹🇿", "official",
                        "坦桑尼亚英语，斯瓦希里语底子"),
}

#: 外语兜底地区（这些地方没有官方英语变体）
FOREIGN_REGIONS: dict[str, RegionMeta] = {
    "it-IT": RegionMeta("it-IT", "意大利", "Italy", "🇮🇹", "foreign_fallback",
                        "意大利腔：元音开、卷舌 R、音节均匀"),
    "fr-FR": RegionMeta("fr-FR", "法国", "France", "🇫🇷", "foreign_fallback",
                        "法国腔：鼻化元音、重音在词尾"),
    "de-DE": RegionMeta("de-DE", "德国", "Germany", "🇩🇪", "foreign_fallback",
                        "德国腔：辅音硬、语调降、断句明显"),
    "es-ES": RegionMeta("es-ES", "西班牙", "Spain", "🇪🇸", "foreign_fallback",
                        "西班牙腔：元音纯、r 颤音"),
    "ja-JP": RegionMeta("ja-JP", "日本", "Japan", "🇯🇵", "foreign_fallback",
                        "日式腔：音节等长、辅音轻柔"),
    "zh-CN": RegionMeta("zh-CN", "中国", "China", "🇨🇳", "foreign_fallback",
                        "中式英语：声调语言底子、辅音尾音弱化"),
}


# --------------------------------------------------------------------------- #
# 声线目录
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AccentOption:
    """口音市场里的一条声线。"""

    voice: str
    locale: str
    label_zh: str        # 中国男声 / 英国女声
    gender: str          # male / female
    kind: str            # official / foreign_fallback
    vibe: str = ""       # 听感描述
    #: 推荐的角色设定（给用户选口音时参考）
    suggested_roles: tuple[str, ...] = field(default_factory=tuple)

    @property
    def region(self) -> RegionMeta:
        return OFFICIAL_REGIONS.get(self.locale) or FOREIGN_REGIONS.get(self.locale) or RegionMeta(
            self.locale, self.locale, self.locale, "🌐", self.kind, ""
        )

    def to_dict(self) -> dict:
        r = self.region
        return {
            "voice": self.voice,
            "locale": self.locale,
            "label": f"{r.flag} {r.label_zh} {self.label_zh}",
            "label_zh": self.label_zh,
            "gender": self.gender,
            "kind": self.kind,
            "kind_label": "官方英语变体" if self.kind == "official" else "外语腔（兜底）",
            "region_zh": r.label_zh,
            "region_en": r.label_en,
            "flag": r.flag,
            "vibe": self.vibe or r.vibe,
            "suggested_roles": list(self.suggested_roles),
        }


def _v(voice: str, gender: str, zh: str, roles: tuple[str, ...] = (), vibe: str = "") -> AccentOption:
    locale = "-".join(voice.split("-")[:2])
    kind = "official" if locale in OFFICIAL_REGIONS else "foreign_fallback"
    return AccentOption(
        voice=voice, locale=locale, label_zh=zh, gender=gender,
        kind=kind, vibe=vibe, suggested_roles=roles,
    )


#: 全量口音目录（都是本机验证过可用的声线）
ACCENT_CATALOG: tuple[AccentOption, ...] = (
    # ---------------- 英国 ----------------
    _v("en-GB-RyanNeural", "male", "男声·Ryan", ("咖啡师", "酒保", "老师"), "温暖、放松，标准 RP"),
    _v("en-GB-ThomasNeural", "male", "男声·Thomas", ("书店老板", "顾问"), "沉稳、略正式"),
    _v("en-GB-SoniaNeural", "female", "女声·Sonia", ("HR", "面试官"), "清晰、职业"),
    _v("en-GB-LibbyNeural", "female", "女声·Libby", ("同事", "房东"), "轻快、亲切"),
    _v("en-GB-MaisieNeural", "female", "女声·Maisie", ("学生", "实习生"), "年轻、活泼"),
    # ---------------- 爱尔兰 ----------------
    _v("en-IE-ConnorNeural", "male", "男声·Connor", ("酒保", "音乐人"), "上扬、有音乐感"),
    _v("en-IE-EmilyNeural", "female", "女声·Emily", ("导游", "作家"), "柔和、轻快"),
    # ---------------- 澳大利亚 ----------------
    _v("en-AU-NatashaNeural", "female", "女声·Natasha", ("冲浪教练", "旅舍老板"), "慵懒、友好"),
    _v("en-AU-WilliamMultilingualNeural", "male", "男声·William", ("牧场主", "探险向导"), "低沉稳重"),
    # ---------------- 新西兰 ----------------
    _v("en-NZ-MitchellNeural", "male", "男声·Mitchell", ("户外教练", "农场主"), "质朴、直接"),
    _v("en-NZ-MollyNeural", "female", "女声·Molly", ("咖啡店店员", "护士"), "明亮、友好"),
    # ---------------- 加拿大 ----------------
    _v("en-CA-LiamNeural", "male", "男声·Liam", ("工程师", "客服"), "平稳、中性"),
    _v("en-CA-ClaraNeural", "female", "女声·Clara", ("设计师", "产品经理"), "清晰、温和"),
    # ---------------- 美国 ----------------
    _v("en-US-AndrewNeural", "male", "男声·Andrew", ("销售", "创业顾问"), "自信、有说服力"),
    _v("en-US-BrianNeural", "male", "男声·Brian", ("同事", "技术支持"), "随和、口语化"),
    _v("en-US-GuyNeural", "male", "男声·Guy", ("记者", "主持人"), "播音腔、有力"),
    _v("en-US-JennyNeural", "female", "女声·Jenny", ("助理", "客服"), "亲切、耐心"),
    _v("en-US-EmmaNeural", "female", "女声·Emma", ("同学", "实习生"), "年轻、自然"),
    _v("en-US-AriaNeural", "female", "女声·Aria", ("招聘官", "面试官"), "干练、专业"),
    # ---------------- 印度 ----------------
    _v("en-IN-PrabhatNeural", "male", "男声·Prabhat", ("工程师", "技术支持"), "语速快、音节计时"),
    _v("en-IN-NeerjaNeural", "female", "女声·Neerja", ("项目经理", "医生"), "清晰、专业"),
    _v("en-IN-NeerjaExpressiveNeural", "female", "女声·Neerja Expressive", ("销售", "主持人"), "表现力强"),
    # ---------------- 新加坡 ----------------
    _v("en-SG-LunaNeural", "female", "女声·Luna", ("PM", "银行职员"), "高效、利落"),
    _v("en-SG-WayneNeural", "male", "男声·Wayne", ("创业者", "顾问"), "平稳、务实"),
    # ---------------- 南非 ----------------
    _v("en-ZA-LeahNeural", "female", "女声·Leah", ("导游", "客服"), "明快、直接"),
    _v("en-ZA-LukeNeural", "male", "男声·Luke", ("向导", "户外教练"), "干脆、硬朗"),
    # ---------------- 香港 ----------------
    _v("en-HK-YanNeural", "female", "女声·Yan", ("同事", "银行职员"), "清晰、克制"),
    _v("en-HK-SamNeural", "male", "男声·Sam", ("金融从业者", "同事"), "务实、平稳"),
    # ---------------- 肯尼亚 ----------------
    _v("en-KE-ChilembaNeural", "male", "男声·Chilemba", ("向导", "野生动物学家"), "低沉稳重"),
    _v("en-KE-AsiliaNeural", "female", "女声·Asilia", ("NGO 工作者", "教师"), "温和、清晰"),
    # ---------------- 尼日利亚 ----------------
    _v("en-NG-AbeoNeural", "male", "男声·Abeo", ("创业者", "律师"), "富有张力"),
    _v("en-NG-EzinneNeural", "female", "女声·Ezinne", ("市场总监", "记者"), "活力充沛"),
    # ---------------- 菲律宾 ----------------
    _v("en-PH-JamesNeural", "male", "男声·James", ("客服", "呼叫中心"), "美音底子、清楚"),
    _v("en-PH-RosaNeural", "female", "女声·Rosa", ("客服", "护士"), "温柔、耐心"),
    # ---------------- 坦桑尼亚 ----------------
    _v("en-TZ-ElimuNeural", "male", "男声·Elimu", ("向导", "教师"), "平稳、清晰"),
    _v("en-TZ-ImaniNeural", "female", "女声·Imani", ("志愿者", "教师"), "温暖、明亮"),
    # ---------------- 意大利（兜底）----------------
    _v("it-IT-GiuseppeMultilingualNeural", "male", "男声·Giuseppe", ("设计师", "主厨"), "意大利腔浓，卷舌 R"),
    _v("it-IT-DiegoNeural", "male", "男声·Diego", ("服务员", "商人"), "沉稳、元音开"),
    _v("it-IT-IsabellaNeural", "female", "女声·Isabella", ("设计师", "店员"), "明亮、有节奏"),
    _v("it-IT-ElsaNeural", "female", "女声·Elsa", ("教师", "艺术家"), "柔和、偏慢"),
    # ---------------- 法国（兜底）----------------
    _v("fr-FR-HenriNeural", "male", "男声·Henri", ("酒保", "厨师"), "鼻化元音、重音词尾"),
    _v("fr-FR-DeniseNeural", "female", "女声·Denise", ("店员", "艺术策展人"), "优雅、有韵律"),
    # ---------------- 德国（兜底）----------------
    _v("de-DE-ConradNeural", "male", "男声·Conrad", ("工程师", "项目主管"), "辅音硬、语调降"),
    _v("de-DE-KatjaNeural", "female", "女声·Katja", ("同事", "招聘官"), "清晰、克制"),
    # ---------------- 西班牙（兜底）----------------
    _v("es-ES-AlvaroNeural", "male", "男声·Alvaro", ("导游", "餐厅老板"), "元音纯、r 颤音"),
    _v("es-ES-ElviraNeural", "female", "女声·Elvira", ("旅店老板", "教师"), "明快、有乐感"),
    # ---------------- 日本（兜底）----------------
    _v("ja-JP-KeitaNeural", "male", "男声·Keita", ("同事", "店员"), "音节等长、轻柔"),
    _v("ja-JP-NanamiNeural", "female", "女声·Nanami", ("店员", "导游"), "温和、清楚"),
    # ---------------- 中国（兜底）----------------
    _v("zh-CN-YunxiNeural", "male", "男声·云希", ("同事", "房东"), "中式英语底子"),
    _v("zh-CN-XiaoxiaoNeural", "female", "女声·晓晓", ("同事", "导游"), "中文腔明显"),
)


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def all_accents() -> list[dict]:
    """全量口音（给前端市场用）。"""
    return [a.to_dict() for a in ACCENT_CATALOG]


def by_kind(kind: str) -> list[dict]:
    return [a.to_dict() for a in ACCENT_CATALOG if a.kind == kind]


def by_locale(locale: str) -> list[dict]:
    return [a.to_dict() for a in ACCENT_CATALOG if a.locale == locale]


def get_accent(voice: str) -> dict | None:
    for a in ACCENT_CATALOG:
        if a.voice == voice:
            return a.to_dict()
    return None


def regions() -> list[dict]:
    """地区列表（含该地区可用声线数）。"""
    counts: dict[str, int] = {}
    for a in ACCENT_CATALOG:
        counts[a.locale] = counts.get(a.locale, 0) + 1

    out = []
    for locale, meta in {**OFFICIAL_REGIONS, **FOREIGN_REGIONS}.items():
        n = counts.get(locale, 0)
        if n == 0:
            continue
        out.append({
            "locale": locale,
            "label_zh": meta.label_zh,
            "label_en": meta.label_en,
            "flag": meta.flag,
            "kind": meta.kind,
            "vibe": meta.vibe,
            "voice_count": n,
        })
    # 官方变体在前
    out.sort(key=lambda r: (r["kind"] != "official", r["label_zh"]))
    return out


def market_summary() -> dict:
    """给前端首屏用的市场概览。"""
    official = [a for a in ACCENT_CATALOG if a.kind == "official"]
    foreign = [a for a in ACCENT_CATALOG if a.kind == "foreign_fallback"]
    return {
        "total": len(ACCENT_CATALOG),
        "official": len(official),
        "foreign_fallback": len(foreign),
        "region_count": len({a.locale for a in ACCENT_CATALOG}),
        "official_region_count": len({a.locale for a in official}),
        "policy": (
            "官方英语变体优先（真实地区口音，100% 自然）；"
            "没有官方变体的地区（意大利/法国/德国/日本/中国）用外语声线兜底。"
        ),
    }
