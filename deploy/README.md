# Sonder 服务器部署与安全升级

公开体验：<https://47-104-24-236.sslip.io>。新用户通过邮箱验证码登录，并使用内测邀请码；获取内测码请联系 [yulinsun547@gmail.com](mailto:yulinsun547@gmail.com)。部署自己的实例时请配置自己的域名、邮件与上游账号。

本指南对应 v4.4.0 的单进程 FastAPI + SQLite WAL 部署。Android 从 APK 内的 `https://localhost` 加载界面，通过 HTTPS 请求 API；网页由同源后端托管。GitHub Release 仅附 [Android APK](https://github.com/LQ-1123/speakeasy/releases/download/v4.4.0/Sonder-v4.4.0-release.apk)，不等于已更新生产服务或服务器 APK 发布目录。

```text
浏览器 / 内置 Web 的 Android APK ──HTTPS──> nginx
                                          ├─ /_next/static/：公开的哈希 JS/CSS
                                          └─ 127.0.0.1:8000：单个 Uvicorn worker
                                               ├─ 账号 / API / 私有媒体授权 / WebSocket
                                               ├─ SQLite WAL + 私有 data/
                                               └─ LLM / Fish / STT / SMTP / 推送
```

上游凭据只由后端持有；必要的文本、音频和邮件等会发送给已配置服务商。不能把“密钥不发给客户端”理解为“数据不离开服务器”。

## 1. 准备与目录

- 使用受支持的 Linux、Python 3.11 / 3.12，按 `backend/requirements-cloud.txt` 安装云精简依赖；不需要在小服务器上安装 torch 或下载本地语音模型。
- 在开发机完成前端静态构建，服务器不运行 Node 构建。2C2G 是需要实际测量的部署目标，不是本指南保证的承载能力。
- nginx 负责 443 TLS，应用仅监听回环地址。按管理策略限制 SSH；不把 8000 或管理端无认证入口暴露到公网。
- 推荐应用由独立非 root 服务账号运行，例如 `speakeasy`；以下 `/srv/speakeasy` 是示例路径，应按现有部署调整。
- `.env`、`data/`、provider 主密钥、签名文件和备份均不得放在公开静态目录。nginx 只需读取 `/var/www/speakeasy/_next/static/`。

初次安装可在已经由管理员创建并授权给服务账号的目录中检出指定版本：

```sh
git clone --branch v4.4.0 https://github.com/LQ-1123/speakeasy.git /srv/speakeasy
cd /srv/speakeasy
python3.12 -m venv .venv-v4.4.0
.venv-v4.4.0/bin/python -m pip install -r backend/requirements-cloud.txt
.venv-v4.4.0/bin/python -m pip check
```

首次安装与升级都不要删除现有虚拟环境；为新版本建立独立环境，验证后调整服务路径，保留旧环境供应用代码回滚。

## 2. 先在开发机构建网页

```sh
cd frontend
npm ci
npm run build:backend
```

产物为 `backend/app/static/app/`。网页同源部署时不要把本机或其他实例的 API 地址打进产物。将已核对版本的应用代码和静态产物暂存到服务器；在维护窗口仅替换这次发布的代码/构建目录。

不要对运行目录执行整目录覆盖或带删除的同步。尤其不能用本机 `.env`、`data/`、空目录、密钥或打包缓存覆盖线上配置及数据。仓库包含的示例 systemd/nginx 文件必须逐项对照现有配置，不直接覆盖正在使用的 unit、TLS 配置或证书。

## 3. 备份：数据库一致性、配置和密钥

SQLite WAL 模式下，逐个复制活动 `.db`、`-wal`、`-shm` 不能获得原子快照。升级前先进入维护窗口、停止应用及所有后台 worker，确认没有其他进程写库，再使用 SQLite backup API。备份目录必须在私有数据根目录与 Web 根目录之外。

以下在服务器由有权限的维护者执行，目录需预先授权给实际执行账号：

```sh
cd /srv/speakeasy
sudo systemctl stop speakeasy
export BACKUP="/srv/backups/speakeasy/$(date +%Y%m%d-%H%M%S)"
umask 077
mkdir -p "$BACKUP"
.venv-v4.4.0/bin/python - <<'PY'
import os
import sqlite3
from pathlib import Path

source = Path('data/speakeasy_v2.db').resolve(strict=True)
target = Path(os.environ['BACKUP']) / 'speakeasy_v2.db'
if target.exists():
    raise SystemExit('Refusing to replace an existing backup')
with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
    with sqlite3.connect(target) as dst:
        src.backup(dst)
        result = dst.execute('PRAGMA integrity_check').fetchall()
        if result != [('ok',)]:
            raise SystemExit('Backup integrity check failed')
print('SQLite backup integrity: ok')
PY
```

继续在停止写入的维护窗口中，备份受保护的 `.env`、配置、媒体、部署 unit/nginx 配置及源码版本，保留原文件权限。**provider 加密数据库与解密主密钥应分别备份**：默认主密钥为 `data/provider-master.key`，如使用 `PROVIDER_POOL_MASTER_KEY`，备份其受保护的秘密配置来源。将主密钥存到独立受控位置，不能只备份数据库，也不能随源码或公开产物上传。

恢复演练应在隔离实例进行，先确认数据库完整性、主密钥可解密、媒体归属和配置齐全。邮箱账号历史归档是另一个需要清单审阅的操作，必须按[邮箱账号上线指南](../docs/EMAIL_CODE_AUTH_ROLLOUT.md)演练；普通代码发布不自动执行归档。

`deploy_v3.sh` 是历史脚本：其逐文件备份活动数据库/WAL及直接覆盖数据库的回滚逻辑不提供一致性保证，也不涵盖完整的主密钥备份。不要用它替代本节的维护与备份流程。

## 4. 正式配置

首次安装时可参考 `.env.production.example`，但它不是完整的正式账号配置；已有实例应按差异编辑，不能覆盖 `.env`。正式邮箱模式必须按[邮箱部署指南](../docs/EMAIL_CODE_AUTH_ROLLOUT.md)配置：

```dotenv
ALLOW_KEY_EDIT=0
SE_EMAIL_CODE_AUTH=1
SE_REQUIRE_AUTH=1
SE_INVITE_REQUIRED=1
SE_MAIL_DEV=0
SE_LEGACY_SHARED_OWNER=0
SE_ADS_ANONYMOUS=0
CORS_ORIGINS=https://your-domain.example.com,https://localhost
```

另在受保护的服务器配置中填写 SMTP SSL 465、验证码签名密钥、媒体密钥、管理员鉴权与所需推送服务端凭据；真实值不写入文档、源码或构建日志。Web 使用 HttpOnly Cookie + CSRF，Android 使用原生加密保存的邮箱会话。不要为兼容旧 APK 重新启用匿名设备注册。

上游配置入口为 **Admin → API 与密钥**。逐个添加、验证并启用资源，按真实账号/项目设置共享配额；Key 多不代表配额独立。验证会发起小型真实业务请求并可能消耗额度。Fish 保持人物音色，严格模式不以提示音冒充合成成功。细节见[多账号资源池指南](../docs/PROVIDER_POOLS.md)。

保持一个应用进程；前台排队、上游租约和通话注册限制含进程内状态。具体并发/队列默认值以[当前资源池指南](../docs/PROVIDER_POOLS.md)为准，旧环境变量可能覆盖新默认值。请求预算是保守次数上限，不能代替真实价格、余额或费用上限。

## 5. 单进程 systemd 与可信代理

`speakeasy.service` 是模板，旧路径为 `/root/speakeasy`。适配 `User`、`Group`、工作目录、虚拟环境和现有秘密加载方式后，再安装服务。非 root 示例的关键设置如下：

```ini
[Service]
User=speakeasy
Group=speakeasy
WorkingDirectory=/srv/speakeasy
Environment=PYTHONPATH=/srv/speakeasy/backend
Environment=PYTHONUNBUFFERED=1
ExecStart=/srv/speakeasy/.venv-v4.4.0/bin/python -m uvicorn app.main:app --app-dir /srv/speakeasy/backend --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=on-failure
RestartSec=3
```

只信任实际反向代理来源；nginx 与应用分机时使用明确的私网代理地址，不设置任意来源 `*`。邮箱 IP 限频依赖正确的客户端地址，HTTPS URL 与媒体票据依赖正确的 `X-Forwarded-Proto`。确保服务账号可读私有配置、可写所需数据目录，provider 主密钥权限为 0600。

由维护者检查变更后的 unit，再执行 `systemctl daemon-reload`、启动服务及查看受控日志。不要把管理员令牌、邮件验证码或完整密钥写进健康检查命令。

## 6. nginx、TLS 与公开静态资源

保留现有域名和证书配置，对照 `nginx.conf` 合并代理、WebSocket 和静态资源规则。模板中的 `server_name` 必须替换；首次部署按运维策略申请并续期 TLS 证书，已有实例不因代码升级重新覆盖 TLS 配置。

nginx worker 通常无法访问 `/root`，也不应获准访问私有数据。只发布前端的公开哈希资源：

```sh
sudo install -d -m 755 /var/www/speakeasy/_next
sudo cp -a backend/app/static/app/_next/static /var/www/speakeasy/_next/
sudo find /var/www/speakeasy/_next/static -type d -exec chmod 755 {} +
sudo find /var/www/speakeasy/_next/static -type f -exec chmod 644 {} +
sudo nginx -t
sudo systemctl reload nginx
```

保留仍被已打开页面引用的旧哈希文件，避免升级期间 JS/CSS 404。不要发布整个 `backend/app/static/app` 的父目录、`data/` 或私有音频别名。认证、音频归属与短期票据仍由 Python API 校验。事件/通话 WebSocket 与流式响应需关闭代理缓冲并转发 Upgrade 头，普通响应可保留缓冲。

## 7. 上线检查与 Android

1. 本机回环 `/api/v3/health`、`/api/v3/auth/config` 返回正常；正式配置显示邮箱模式与邀请开关。
2. 公网 HTTPS 首页、冷缓存 JS/CSS、静态 MIME、证书及 WebSocket 正常。
3. 使用专用测试邮箱核验真实收信、邀请码首次注册、同邮箱登录复用、会话撤销与数据隔离；不要把验证码写进报告。
4. 已认证聊天、该条回复的可播放音频、重试、事件连接和通话按配置工作；未认证/越权媒体访问被拒绝。
5. 对照资源池的实际验证状态、配额与后台任务检查日志，确认没有把 mock 或占位音频当作真实服务成功。

Android 发布必须通过 `mobile/build-apk.ps1` 重新构建前端、同步并打包，使用原包名与原签名。API 必须为 HTTPS；CORS 只加入实际客户端来源（当前内置 Web 为 `https://localhost`）。参见 [Android 指南](../mobile/README.md)和[更新渠道说明](../docs/APP_UPDATE.md)。GitHub v4.4.0 发布不自动更新 `/api/app/latest`、服务器 APK 目录或宣传页。

## 8. 回滚与容量边界

应用回滚优先恢复兼容当前数据库及邮箱会话的旧代码/虚拟环境，保留 `.env`、`data/`、provider 主密钥、unit 与 TLS。回滚前再次停止写入并保存当前一致性快照。不要在有新账号/消息后直接用旧数据库覆盖；需要数据恢复时先在隔离实例核对增量与迁移方案。恢复数据库必须停止所有写入进程，并按 SQLite 恢复流程处理当前数据库及 WAL 状态，不能只覆盖主 `.db`。

当前证据：开发机回环假上游模拟 100 用户、3.33 轮/秒、30 分钟共 6,000/6,000 轮音频可解码且在期限内完成；该长测属于启动时快照，测量期间工作树有修复。最终冻结源码另做 60 秒 200/200 复测。它们不证明真实 2C2G Linux 容量、真实供应商额度、音色质量或月费用上限。上线容量需要在实际资源限制和真实账号条件下单独验收，见[资源池证据](../docs/PROVIDER_POOLS.md)与[容量测量方法](../docs/PERFORMANCE_CAPACITY.md)。
