"""微调头像配色/配件概率：去掉接近白发的老气色，降低眼镜概率。"""

import io

p = "backend/app/v3/avatar.py"
s = io.open(p, encoding="utf-8").read()

s = s.replace(
    'HAIR_COLORS = ("2c1b18", "4a312c", "724133", "a55728", "b58143", "d6b370", "e8e1e1")',
    'HAIR_COLORS = ("2c1b18", "3a2418", "4a312c", "5a3a24", "724133", "8a4b2a", "a55728", "b58143")',
)
s = s.replace("has_glasses = p.chance(24)", "has_glasses = p.chance(16)")
s = s.replace(
    'has_beard = gender == "male" and p.chance(26)',
    'has_beard = gender == "male" and p.chance(22)',
)
io.open(p, "w", encoding="utf-8").write(s)
print("hair palette:", "e8e1e1" in s)
print("glasses chance 16:", "p.chance(16)" in s)
