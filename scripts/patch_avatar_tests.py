"""更新测试：头像从 mock 图片改为种子生成。"""

import io

p = "backend/tests/test_v3_world.py"
s = io.open(p, encoding="utf-8").read()
s = s.replace(
    'assert persona["avatar"].startswith("/mock/avatars/")',
    'assert persona["avatar"].startswith("/api/v3/avatar/p_")',
)
s = s.replace(
    'assert data["persona"]["avatar"].startswith("/mock/avatars/")',
    'assert data["persona"]["avatar"].startswith("/api/v3/avatar/p_")',
)
io.open(p, "w", encoding="utf-8").write(s)
print("updated:", s.count('/api/v3/avatar/p_'))
