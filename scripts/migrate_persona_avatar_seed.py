"""一次性迁移：Persona 头像从 mock 图片改为种子生成（avatar_seed）。"""

import io
import re

p = "backend/app/v3/seed_data.py"
s = io.open(p, encoding="utf-8").read()
s2, n = re.subn(
    r'"avatar": "/mock/avatars/([a-z]+)\.jpg"',
    r'"avatar_seed": "persona-\1"',
    s,
)
io.open(p, "w", encoding="utf-8").write(s2)
print("replaced avatar -> avatar_seed:", n)
print("remaining mock avatar refs:", len(re.findall(r"/mock/avatars/", s2)))
