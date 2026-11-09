"""把代码里现有的词表导出成 ``data/lexicons/*.json``（词库管理的初始数据）。

跑一次即可：之后运营在管理台改 JSON，代码不再需要改。

    python scripts/export_lexicons.py            # 只写缺失的文件
    python scripts/export_lexicons.py --force    # 覆盖已有文件（会先备份）
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.v3 import lexicon, speech_dna  # noqa: E402


def build_dna() -> dict:
    fields: dict[str, dict] = {}
    for field in speech_dna.DNA_KEYS:
        trait_map = speech_dna._TRAIT_BY_FIELD.get(field)  # noqa: SLF001 - 导出一次性使用
        values: dict[str, dict] = {}
        for value in speech_dna.DNA_ENUMS.get(field, ()):
            meta: dict = {"enabled": True}
            if trait_map is not None and value in trait_map:
                meta["trait"] = round(float(trait_map[value]), 3)
            values[value] = meta
        fields[field] = {
            "values": values,
            "default": speech_dna.DEFAULT_DNA.get(field, ""),
        }
    return {"version": 1, "fields": fields, "note": "枚举值只能停用、不能删除；新增必须带 trait 数值（0..1）"}


def build_scenes() -> dict:
    return {
        "version": 1,
        "scenes": [{"key": key} for key in speech_dna.SCENES],
        "note": "示例对话按场景覆盖；少于 8 个场景会被校验拒绝",
    }


def build_phrases() -> dict:
    return {
        "version": 1,
        "banned_global": [
            "as an AI",
            "as a language model",
            "How can I help you today",
            "I'm just a program",
        ],
        "signature_pools": {
            "en": ["right", "honestly", "I mean", "look", "fair enough", "anyway"],
            "de": ["also", "na ja", "echt", "genau", "ach"],
            "ja": ["まあね", "そうだね", "えっと", "ていうか"],
            "ko": ["아 진짜", "음", "그니까", "헐"],
            "pt": ["gente", "olha", "sabe", "tipo"],
            "es": ["bueno", "o sea", "mira", "en fin"],
            "it": ["boh", "cioè", "insomma"],
            "zh": ["其实", "说真的", "反正", "就那样"],
        },
        "note": "signature_pools 供新角色生成时参考；banned_global 会注入每个角色的 prompt",
    }


def build_world() -> dict:
    names: dict[str, list[dict]] = {}
    interests: set[str] = set()
    cities: dict[str, list[str]] = {}
    occupations: set[str] = set()
    try:
        from app.v3 import seed_data  # noqa: PLC0415

        for persona in seed_data.PERSONAS:
            lang = str(persona.get("language") or persona.get("native_lang") or "en")
            nick = str(persona.get("nickname") or persona.get("name") or "").strip()
            if nick:
                names.setdefault(lang, [])
                exists = any(
                    (item.get("name") if isinstance(item, dict) else item) == nick
                    for item in names[lang]
                )
                if not exists:
                    names[lang].append(
                        {
                            "name": nick,
                            "gender": str(persona.get("gender") or ""),
                            "country": str(persona.get("country") or ""),
                        }
                    )
            interests.update(str(x) for x in (persona.get("interests") or []))
            if persona.get("city"):
                _country = str(persona.get("country") or "any").strip() or "any"
                cities.setdefault(_country, [])
                if str(persona["city"]) not in cities[_country]:
                    cities[_country].append(str(persona["city"]))
            if persona.get("occupation"):
                occupations.add(str(persona["occupation"]))
    except Exception:  # noqa: BLE001
        pass
    for lang in ("en", "ja", "de", "ko", "pt", "es", "it", "zh"):
        names.setdefault(lang, [])
    return {
        "version": 2,
        "names": names,
        "occupations": {"any": sorted(occupations)},
        "cities": cities,
        "interests": {"any": sorted(interests)},
        "note": "捏人素材池：名字按语言、其余按分类；空数组表示该池暂未配置",
    }


BUILDERS = {
    "dna": build_dna,
    "scenes": build_scenes,
    "phrases": build_phrases,
    "world": build_world,
}


def main() -> int:
    force = "--force" in sys.argv
    for name, builder in BUILDERS.items():
        path = lexicon.path_for(name)
        if os.path.exists(path) and not force:
            print(f"跳过（已存在）: {name}")
            continue
        data = builder()
        issues = lexicon.validate(name, data)
        if issues:
            print(f"!! {name} 导出自检未通过: {issues}")
            return 1
        if os.path.exists(path):
            backup = path[: -len(".json")] + ".bak.json"
            os.replace(path, backup)
            print(f"已备份旧版: {os.path.basename(backup)}")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"已导出: {path}")
    applied = lexicon.refresh()
    print(f"应用结果: {applied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
