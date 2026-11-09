"""补充捏人素材：每个国家 +5 个名字（带性别/国家），并给每个国家补 4 个城市。

通过管理端 API 写入（会自动校验 + 备份 + 热生效），不直接改文件。

    python scripts/add_world_content.py
"""

from __future__ import annotations

import os
import sys

import httpx

BASE = os.getenv("SE_ACCEPT_BASE", "http://127.0.0.1:8000")

#: 每个国家新增 5 个名字（性别混合，母语更自然）
NEW_NAMES: dict[str, list[tuple[str, str]]] = {
    "United Kingdom": [("Oliver", "male"), ("Amelia", "female"), ("Harry", "male"),
                       ("Isla", "female"), ("George", "male")],
    "United States": [("Noah", "male"), ("Ava", "female"), ("Liam", "male"),
                      ("Mia", "female"), ("Ethan", "male")],
    "Australia": [("Jack", "male"), ("Charlotte", "female"), ("Lachlan", "male"),
                  ("Matilda", "female"), ("Hamish", "male")],
    "Ireland": [("Cian", "male"), ("Saoirse", "female"), ("Oisin", "male"),
                ("Niamh", "female"), ("Darragh", "male")],
    "Japan": [("Sakura", "female"), ("Ren", "male"), ("Yui", "female"),
              ("Kaito", "male"), ("Aoi", "female")],
    "Korea": [("Minjun", "male"), ("Seoyeon", "female"), ("Hyunwoo", "male"),
              ("Jieun", "female"), ("Doyun", "male")],
    "Singapore": [("Zixuan", "male"), ("Wanting", "female"), ("Haoran", "male"),
                  ("Yuxin", "female"), ("Jun", "male")],
    "Germany": [("Jonas", "male"), ("Lena", "female"), ("Felix", "male"),
                ("Hannah", "female"), ("Elias", "male")],
    "Brazil": [("Rafael", "male"), ("Beatriz", "female"), ("Lucas", "male"),
               ("Juliana", "female"), ("Thiago", "male")],
    "Italy": [("Marco", "male"), ("Sofia", "female"), ("Luca", "male"),
              ("Chiara", "female"), ("Matteo", "male")],
    "Spain": [("Pablo", "male"), ("Lucia", "female"), ("Javier", "male"),
              ("Carmen", "female"), ("Diego", "male")],
}

#: 每个国家新增 4 个城市
NEW_CITIES: dict[str, list[str]] = {
    "United Kingdom": ["London", "Bristol", "Edinburgh", "Glasgow"],
    "United States": ["New York", "Chicago", "Portland", "Denver"],
    "Australia": ["Sydney", "Brisbane", "Perth", "Hobart"],
    "Ireland": ["Cork", "Galway", "Limerick", "Kilkenny"],
    "Japan": ["Kyoto", "Osaka", "Fukuoka", "Sapporo"],
    "Korea": ["Busan", "Incheon", "Daegu", "Gwangju"],
    "Singapore": ["Jurong", "Tampines", "Woodlands", "Bedok"],
    "Germany": ["Berlin", "Munich", "Cologne", "Leipzig"],
    "Brazil": ["Rio de Janeiro", "Belo Horizonte", "Curitiba", "Porto Alegre"],
    "Italy": ["Rome", "Milan", "Florence", "Turin"],
    "Spain": ["Madrid", "Barcelona", "Seville", "Bilbao"],
}

#: 国家 → 语言分组（名字写进对应语言池）
COUNTRY_LANG: dict[str, str] = {
    "United Kingdom": "en", "United States": "en", "Australia": "en", "Ireland": "en",
    "Japan": "ja", "Korea": "ko", "Singapore": "zh", "Germany": "de",
    "Brazil": "pt", "Italy": "it", "Spain": "es",
}


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=60.0) as client:
        current = client.get("/api/admin/lexicons/world").json()
        data = current["data"]
        names = data.setdefault("names", {})
        cities = data.setdefault("cities", {})

        added_names = 0
        added_cities = 0
        for country, people in NEW_NAMES.items():
            lang = COUNTRY_LANG[country]
            group = names.setdefault(lang, [])
            existing = {
                (item.get("name") if isinstance(item, dict) else item)
                for item in group
            }
            for name, gender in people:
                if name in existing:
                    continue
                group.append({"name": name, "gender": gender, "country": country})
                existing.add(name)
                added_names += 1

        for country, extra in NEW_CITIES.items():
            group = cities.setdefault(country, [])
            for city in extra:
                if city not in group:
                    group.append(city)
                    added_cities += 1

        result = client.put("/api/admin/lexicons/world", json={"data": data}).json()
        if not result.get("ok"):
            print("保存失败:", result.get("issues"))
            return 1
        print(f"新增名字 {added_names} 个、城市 {added_cities} 个")

        fresh = client.get("/api/admin/lexicons/world").json()["data"]
        print("\n名字池（按国家统计）：")
        by_country: dict[str, int] = {}
        for group in fresh.get("names", {}).values():
            for item in group:
                country = item.get("country") or "?"
                by_country[country] = by_country.get(country, 0) + 1
        for country in sorted(by_country):
            print(f"  {country:<16} {by_country[country]} 人")
        print("\n城市池：")
        for country, group in sorted(fresh.get("cities", {}).items()):
            print(f"  {country:<16} {len(group)} 个城市：{', '.join(group[:6])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
