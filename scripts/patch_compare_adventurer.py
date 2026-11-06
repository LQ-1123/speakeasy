"""对比页加入 Adventurer（按地域传 skinColor + 自然发色），用于评估人种/性别表现。"""

import io

p = "docs/avatar-lab-compare.html"
s = io.open(p, encoding="utf-8").read()

# 表头加一列
s = s.replace(
    '<thead><tr><th>人物</th><th>性别 · 年龄 · 国家</th><th>Notionists</th><th>SeedFace（本项目）</th></tr></thead>',
    '<thead><tr><th>人物</th><th>性别 · 年龄 · 国家</th><th>Notionists<br><small>CC0 线稿</small></th>'
    '<th>Adventurer<br><small>CC BY 4.0</small></th><th>SeedFace<br><small>本项目</small></th></tr></thead>',
)

anchor = """      const notionists = AvatarLab.dicebearSvg("notionists", seed);
      const seedface = AvatarLab.customSvg("seed-face", seed, { gender, region, age });"""
assert anchor in s

new_js = """      const notionists = AvatarLab.dicebearSvg("notionists", seed);
      const seedface = AvatarLab.customSvg("seed-face", seed, { gender, region, age });

      // Adventurer：DiceBear 里最能体现「人种」的画风之一（有 skinColor）
      const SKIN = {
        east_asian: ["f5d0c5", "f0c8b4", "e8b89a"],
        south_asian: ["c68642", "a8683a", "8d5524"],
        latino: ["e0ac69", "c68642", "d4956a"],
        black: ["8d5524", "6b4226", "a26a42"],
        white: ["ffdbac", "f1c27d", "e0ac69"],
      };
      const HAIR = ["2c1b18", "4a312c", "724133", "a55728", "b58143", "e5d7a3"];
      const adventurer = AvatarLab.dicebearSvgRaw
        ? AvatarLab.dicebearSvgRaw("adventurer", seed, {
            skinColor: SKIN[region] || SKIN.white,
            hairColor: HAIR,
            earringsProbability: gender === "female" ? 35 : 0,
            glassesProbability: 0,
          })
        : "";"""

s = s.replace(anchor, new_js)

# 行渲染加一列
old_row = """      tr.innerHTML =
        '<td class="name">' + name + '</td>' +
        '<td class="meta">' + gender + ' · ' + age + ' · ' + country + '</td>' +
        '<td><div class="cell"><img src="data:image/svg+xml;charset=utf-8,' + encodeURIComponent(notionists) + '"></div></td>' +
        '<td><div class="cell"><img src="data:image/svg+xml;charset=utf-8,' + encodeURIComponent(seedface) + '"></div></td>';"""
assert old_row in s
new_row = """      tr.innerHTML =
        '<td class="name">' + name + '</td>' +
        '<td class="meta">' + gender + ' · ' + age + ' · ' + country + '</td>' +
        '<td><div class="cell"><img src="data:image/svg+xml;charset=utf-8,' + encodeURIComponent(notionists) + '"></div></td>' +
        '<td><div class="cell"><img src="data:image/svg+xml;charset=utf-8,' + encodeURIComponent(adventurer) + '"></div></td>' +
        '<td><div class="cell"><img src="data:image/svg+xml;charset=utf-8,' + encodeURIComponent(seedface) + '"></div></td>';"""
s = s.replace(old_row, new_row)

# 页面说明补一句 Adventurer 能力
s = s.replace(
    "性别只能靠发型与胡子概率暗示。下面是同一批人物（同 seed）在两种画风下的样子。",
    "性别只能靠发型与胡子概率暗示。Adventurer 有 skinColor，可体现人种，但没有年龄、性别控制也很弱。"
    "下面是同一批人物（同 seed）在三种画风下的样子。",
)

io.open(p, "w", encoding="utf-8").write(s)
print("compare page patched:", "adventurer" in s)
