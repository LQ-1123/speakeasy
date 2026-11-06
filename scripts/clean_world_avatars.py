"""清理 world.py 里对 mock 头像的引用（改为种子）。"""

import io
import re

p = "backend/app/v3/world.py"
s = io.open(p, encoding="utf-8").read()

s = re.sub(r"_AVATARS = \[[\s\S]*?\]\n\n", "", s, count=1)
s = re.sub(r"\n\s*data\.setdefault\(\"avatar\", _AVATARS\[index % len\(_AVATARS\)\]\)", "", s)
s = s.replace('{"nickname","avatar","bio"', '{"nickname","bio"')
s = s.replace('"nickname","avatar","bio"', '"nickname","bio"')

io.open(p, "w", encoding="utf-8").write(s)
print("_AVATARS left:", s.count("_AVATARS"))
print("mock avatar left:", s.count("/mock/avatars/"))
