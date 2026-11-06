"""对比页加入 Avataaars：按地域传 skinColor、按性别挑 top、按年龄调眼镜概率。"""

import io

p = "docs/avatar-lab-compare.html"
s = io.open(p, encoding="utf-8").read()

# 表头再加一列
s = s.replace(
    '<th>Adventurer<br><small>CC BY 4.0</small></th><th>SeedFace<br><small>本项目</small></th>',
    '<th>Adventurer<br><small>CC BY 4.0</small></th>'
    '<th>Avataaars<br><small>免费商用</small></th><th>SeedFace<br><small>本项目</small></th>',
)
s = s.replace(
    "Adventurer 有 skinColor，可体现人种，但没有年龄、性别控制也很弱。",
    "Adventurer 有 skinColor，可体现人种，但没有年龄、性别控制也很弱；"
    "Avataaars 的部件最全（肤色/发型/胡子/眼镜都可指定），最接近「可控的卡通人像」。",
)

anchor = """      const adventurer = AvatarLab.dicebearSvgRaw"""
assert anchor in s

# 在 adventurer 之后追加 avataaars
insert_after = """            glassesProbability: 0,
          })
        : "";"""
assert insert_after in s

avataaars_js = '''

      // Avataaars：部件最全，肤色/发型/胡子/眼镜都可按人物属性指定
      const AV_SKIN = {
        east_asian: ["ffdbb4", "edb98a", "f8d25c"],
        south_asian: ["ae5d29", "d08b5b"],
        latino: ["d08b5b", "ae5d29", "edb98a"],
        black: ["614335"],
        white: ["ffdbb4", "f8d25c"],
      };
      const AV_TOP_F = ["longButNotTooLong", "bob", "bun", "curly", "straight01", "straight02", "bigHair", "miaWallace", "frida"];
      const AV_TOP_M = ["shortFlat", "shortRound", "shortCurly", "shortWaved", "theCaesar", "theCaesarAndSidePart", "sides", "shaggy", "frizzle"];
      const AV_HAIR = ["a55728", "2c1b18", "b58143", "724133", "4a312c", "d6b370"];
      const avAge = age < 25 ? "young" : age < 33 ? "adult" : "mature";
      const avataaars = AvatarLab.dicebearSvgRaw("avataaars", seed, {
        skinColor: AV_SKIN[region] || AV_SKIN.white,
        top: gender === "female" ? AV_TOP_F : AV_TOP_M,
        topProbability: 100,
        hairColor: avAge === "mature" ? AV_HAIR.concat(["e8e1e1", "ecdcbf"]) : AV_HAIR,
        facialHairProbability: gender === "male" ? 25 : 0,
        accessoriesProbability: { young: 8, adult: 15, mature: 40 }[avAge],
        accessoriesColor: ["262e33", "65c9ff", "5199e4"],
        backgroundColor: ["e8f3ec", "eaf0f8", "f8efe6", "f3eaf3", "eef3e6"],
      });'''

s = s.replace(insert_after, insert_after + avataaars_js, 1)

# 行渲染再加一列
old_row = """        '<td><div class="cell"><img src="data:image/svg+xml;charset=utf-8,' + encodeURIComponent(adventurer) + '"></div></td>' +
        '<td><div class="cell"><img src="data:image/svg+xml;charset=utf-8,' + encodeURIComponent(seedface) + '"></div></td>';"""
assert old_row in s
new_row = """        '<td><div class="cell"><img src="data:image/svg+xml;charset=utf-8,' + encodeURIComponent(adventurer) + '"></div></td>' +
        '<td><div class="cell"><img src="data:image/svg+xml;charset=utf-8,' + encodeURIComponent(avataaars) + '"></div></td>' +
        '<td><div class="cell"><img src="data:image/svg+xml;charset=utf-8,' + encodeURIComponent(seedface) + '"></div></td>';"""
s = s.replace(old_row, new_row)

s = s.replace(
    '<h1>Notionists / Adventurer / SeedFace · 12 位 Persona 试穿</h1>',
    '<h1>Notionists / Adventurer / Avataaars / SeedFace · 12 位 Persona 试穿</h1>',
)
s = s.replace(
    '<title>Notionists / Adventurer / SeedFace · 12 位人物试穿</title>',
    '<title>Notionists / Adventurer / Avataaars / SeedFace · 12 位人物试穿</title>',
)

io.open(p, "w", encoding="utf-8").write(s)
print("avataaars column added:", "AV_SKIN" in s)
