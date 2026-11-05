# SpeakEasy 服务器部署（单用户）

目标结构：

```
手机（网页/APK/EXE）  ──HTTPS──▶  你的服务器
                                  ├─ Nginx（443 → 127.0.0.1:8000）
                                  ├─ FastAPI（后端，持有 LLM/Fish/STT 的 key）
                                  ├─ data/（数据库、语音文件、key 配置）
                                  └─ 出站走服务器 VPN → DeepSeek / Fish
```

> key 全部在服务器：手机端只拿到文字和音频 URL，**手机不需要 VPN**。

---

## 0. 前置条件

- 服务器：阿里云 `47.104.24.236`（Linux）
- 安全组放行：`22`（SSH）、`80`、`443`
- 域名：`your-domain.example.com` 解析到服务器 IP（没有域名可先用 `http://IP:8000` 调试，但**手机麦克风必须 HTTPS**）

---

## 1. 传代码到服务器

在服务器上：

```bash
# 方式 A：git（推荐，后续更新方便）
cd /root
git clone https://github.com/LQ-1123/speakeasy.git
cd speakeasy

# 方式 B：本地打包上传（二选一）
# 本地执行：rsync -av --exclude .git --exclude frontend/node_modules ./ root@47.104.24.236:/root/speakeasy/
```

## 2. 安装 Python 依赖

```bash
cd /root/speakeasy
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r backend/requirements.txt -r backend/requirements-tts.txt
```

> 不需要本地 TTS 的话，可以只装 `backend/requirements.txt`，并在 `.env` 里把 TTS 配置成 Fish（默认就是）。

## 3. 把本机的 data 目录传上去（包含 key、数据库、语音）

```bash
# 本地执行（Windows 用 Git Bash / PowerShell 的 scp 都可以）
scp -r data/* root@47.104.24.236:/root/speakeasy/data/
```

`data/` 里必须有：

```
data/speakeasy_v2.db          # 好友/聊天记录
data/llm_config.json          # LLM key
data/tts_config.json          # Fish key
data/audio/                   # 语音文件（可留空）
```

## 4. 配置环境变量

```bash
cd /root/speakeasy
cp deploy/.env.production.example .env
nano .env
```

至少改这几项：

```
ALLOW_KEY_EDIT=0
CORS_ORIGINS=https://your-domain.example.com
# 如果 VPN 是本地代理：
# HTTPS_PROXY=http://127.0.0.1:7890
```

## 5. 先手动跑起来验证

```bash
cd /root/speakeasy
PYTHONPATH=/root/speakeasy/backend .venv/bin/python -m uvicorn app.main:app \
  --app-dir backend --host 127.0.0.1 --port 8000 --log-level info
```

另开一个终端：

```bash
curl -s http://127.0.0.1:8000/api/v2/health | head -c 400
# 期望：{"status":"ok"...,"llm":{"configured":true,...},"tts":...}
```

再发一条文字消息测真实调用（把 `<friend_id>` 换成你库里某个好友 id，或先 `GET /api/v2/bootstrap` 拿）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v2/chat/<friend_id>/text \
  -H 'Content-Type: application/json' -d '{"text":"Hi, how are you?"}' | head -c 300
```

看到正常英文回复就说明 LLM/VPN 通了。语音消息再测一条，确认 STT + TTS 都正常。

## 6. 注册为系统服务（开机自启）

```bash
cp deploy/speakeasy.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now speakeasy
systemctl status speakeasy --no-pager
```

## 7. Nginx + HTTPS

```bash
apt install -y nginx certbot python3-certbot-nginx
cp deploy/nginx.conf /etc/nginx/sites-available/speakeasy
sed -i 's/your-domain.example.com/你的真实域名/g' /etc/nginx/sites-available/speakeasy
ln -sf /etc/nginx/sites-available/speakeasy /etc/nginx/sites-enabled/speakeasy
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# 自动申请证书 + 改写 443
certbot --nginx -d 你的真实域名
```

验证：

```
https://你的域名/api/v2/health  → 200
```

## 8. 前端怎么用

### 网页版（最简单，同源）

后端默认会把 `backend/app/static/app` 一起发出来（`serve_frontend=True`），所以：

```
https://你的域名/  → 直接打开就是 App
```

不需要任何构建参数。

### 分离 / APK

APK 会自带前端静态文件，需要构建时指定后端地址：

```bash
cd frontend
NEXT_PUBLIC_API_BASE=https://你的域名 npm run build:backend
# 产物在 backend/app/static/app，之后打进 Capacitor/Electron 壳
```

`NEXT_PUBLIC_API_BASE` 留空 = 同源；填域名 = 所有 API 和语音 URL 都指向该域名。

---

## 常见问题

| 现象 | 原因/处理 |
|---|---|
| 手机点录音没反应 | 页面不是 HTTPS；麦克风要求安全上下文 |
| 请求报 CORS | `.env` 的 `CORS_ORIGINS` 没包含前端域名（含 `capacitor://localhost`） |
| 通话回复卡着一次性出来 | Nginx 没关 `proxy_buffering` |
| 500 + `api.deepseek.com` 超时 | 服务器代理没生效，检查 `.env` 的 `HTTPS_PROXY` 或 VPN 全局模式 |
| 修改 key 提示 403 | 线上 `ALLOW_KEY_EDIT=0`，改服务器 `.env` 或 `data/*_config.json` 后重启服务 |

## 更新代码

```bash
cd /root/speakeasy
git pull
systemctl restart speakeasy
```

---

## 附：云精简版部署（2 核 2G 小服务器，无 torch）

如果你的服务器配置很低（如 2 核 2G），**不要装完整的 requirements.txt**（会拉 500MB+ 的 torch 和本地语音模型），用精简版：

`ash
# 1) 装 Python 3.12（服务器自带 3.14 太新，很多轮子没有）
curl -LsSf https://astral.sh/uv/install.sh | sh
source \C:\Users\Administrator/.local/bin/env
uv python install 3.12

# 2) 建环境（不要用系统 python3）
cd /root/speakeasy
rm -rf .venv
uv venv --python 3.12 .venv
source .venv/bin/activate

# 3) 只装云精简依赖（约 100MB，1 分钟内）
pip install -r backend/requirements-cloud.txt

# 4) 其余部署步骤和上面一样（传 key 配置 → .env → systemd）
`

启动日志里出现下面这些提示是**正常的**，因为精简版没有本地语音模型：

`
STT 降级到 mock 后端（faster_whisper import 失败）
TTS 后端 cosyvoice2/f5tts/xtts 不可用
`

实际使用中：

- 语音识别 → 云端 SiliconFlow（data/stt_config.json 里配置的 key）
- 语音合成 → 云端 Fish（data/tts_config.json 里配置的 key）

内存占用：空载约 200–350MB，2G 内存足够。

> 注意：精简版不要用 python3.14，numpy/scipy/pydantic 的 3.14 轮子可能缺失；统一用 uv 装的 3.12。
