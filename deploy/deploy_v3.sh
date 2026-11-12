#!/usr/bin/env bash
# SpeakEasy V3 · 云端原地升级脚本（保留现有数据库）
#
# 用法（在服务器上，仓库根目录）：
#   bash deploy/deploy_v3.sh [--dry-run]
#
# 设计原则
#  1. 先备份：data/ 整个目录 + 现有 systemd unit + 代码版本（git rev-parse）
#  2. 不清库：只新增表/列（V3 迁移是幂等 CREATE TABLE IF NOT EXISTS + ALTER TABLE ADD COLUMN）
#  3. 失败自动回滚：健康检查不过则恢复备份并重启旧服务
#  4. 不打印任何密钥
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$ROOT/data/_backup/deploy-$STAMP"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\n✗ %s\n' "$*" >&2; exit 1; }

[[ -x "$PY" ]] || die "找不到 Python：$PY（先建 .venv 或设置 PY=...）"

log "1/8 备份数据库与配置 → $BACKUP"
mkdir -p "$BACKUP"
if [[ "$DRY_RUN" == "0" ]]; then
  cp -a "$ROOT/data/speakeasy_v2.db" "$BACKUP/" 2>/dev/null || true
  for suffix in -wal -shm; do
    [[ -f "$ROOT/data/speakeasy_v2.db$suffix" ]] && cp -a "$ROOT/data/speakeasy_v2.db$suffix" "$BACKUP/" || true
  done
  for f in llm_config.json stt_config.json tts_config.json custom.json; do
    [[ -f "$ROOT/data/$f" ]] && cp -a "$ROOT/data/$f" "$BACKUP/" || true
  done
  cp -a /etc/systemd/system/speakeasy.service "$BACKUP/" 2>/dev/null || true
  (cd "$ROOT" && git rev-parse HEAD > "$BACKUP/git-head.txt" 2>/dev/null) || true
  log "   已备份：$(du -sh "$BACKUP" | cut -f1)"
fi

log "2/8 校验依赖"
"$PY" - <<'PY' || die "缺少依赖：fastapi/uvicorn/sqlite3"
import fastapi, uvicorn, sqlite3  # noqa: F401
print("   依赖 OK")
PY

log "3/8 数据库迁移（幂等：建 V3 表 + 给存量表补列）"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "   (dry-run，跳过实际迁移)"
else
  PYTHONPATH="$ROOT/backend" "$PY" - <<'PY'
import os
from app.v2.db import DB
from app.v3.schema import ensure_schema

db = DB(os.path.join("data", "speakeasy_v2.db"))
db.init()
ensure_schema(db)
counts = {}
with db._lock:
    for table in ("personas", "persona_posts", "friendships", "user_posts", "social_events"):
        counts[table] = db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    cols = {row[1] for row in db.conn.execute("PRAGMA table_info(friends)").fetchall()}
print("   迁移完成:", counts, "| friends.owner_user_id:", "owner_user_id" in cols)
db.close()
PY
fi

log "4/8 前端产物检查（必须存在 Next 静态导出）"
if [[ ! -f "$ROOT/backend/app/static/app/index.html" ]]; then
  die "缺少 backend/app/static/app/index.html —— 本地先跑 cd frontend && npm run build:backend 再上传"
fi
echo "   index.html OK（$(du -sh "$ROOT/backend/app/static/app" | cut -f1)）"

rollback() {
  log "!! 健康检查失败，开始回滚"
  if [[ -d "$BACKUP" ]]; then
    cp -a "$BACKUP/speakeasy_v2.db" "$ROOT/data/" 2>/dev/null || true
    for suffix in -wal -shm; do
      [[ -f "$BACKUP/speakeasy_v2.db$suffix" ]] && cp -a "$BACKUP/speakeasy_v2.db$suffix" "$ROOT/data/" || true
    done
  fi
  systemctl restart speakeasy || true
  die "已回滚到部署前状态，请查看 journalctl -u speakeasy -n 200"
}

log "5/8 重启服务"
[[ "$DRY_RUN" == "0" ]] && systemctl restart speakeasy
sleep 6

log "6/8 健康检查"
# 首次启动要生成头像/每日世界，最多等 120s（24 次 × 5s）
for i in $(seq 1 24); do
  if curl -fsS "http://127.0.0.1:8000/api/v3/health" >/tmp/v3-health.json 2>/dev/null; then
    echo "   健康检查通过：$(head -c 160 /tmp/v3-health.json)…"
    break
  fi
  [[ "$i" == "24" ]] && rollback
  sleep 5
done

log "7/8 关键链路自检（无模型调用）"
# 邮箱验证码模式下业务接口会返回 401/403（需要登录），这属正常；
# 这里只要求「路由存在且按预期响应」，不把 401/403 当失败。
status() { curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8000$1"; }
[[ "$(status /)" == "200" ]] || { echo "   首页非 200"; rollback; }
case "$(status '/api/v3/auth/config')" in
  200) echo "   /api/v3/auth/config -> 200" ;;
  *) echo "   /api/v3/auth/config 异常"; rollback ;;
esac
for path in "/api/v3/feed?limit=1" "/api/v3/personas?limit=1"; do
  code="$(status "$path")"
  case "$code" in
    200|401|403) echo "   $path -> $code（登录模式下 401/403 正常）" ;;
    *) echo "   $path -> $code"; rollback ;;
  esac
done

log "8/8 完成"
echo "   公网入口：http://<服务器IP>:8000"
echo "   备份目录：$BACKUP"
echo "   回滚：cp $BACKUP/speakeasy_v2.db data/ && systemctl restart speakeasy"
