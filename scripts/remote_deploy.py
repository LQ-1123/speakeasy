"""把本地部署包上传到云服务器并原地升级（保留 data/）。"""

import os
import stat
import sys
import time

import paramiko

HOST = os.getenv("SE_DEPLOY_HOST", "47.104.24.236")
USER = os.getenv("SE_DEPLOY_USER", "root")
PASS = os.getenv("SE_DEPLOY_PASS", "")
REMOTE = "/root/speakeasy"
TAR_LOCAL = os.getenv("SE_DEPLOY_TAR", "")
REMOTE_TAR = "/root/speakeasy-deploy.tar.gz"
STAMP = time.strftime("%Y%m%d-%H%M%S")

if not PASS:
    raise SystemExit("缺少 SE_DEPLOY_PASS")
if not TAR_LOCAL or not os.path.isfile(TAR_LOCAL):
    raise SystemExit(f"部署包不存在: {TAR_LOCAL}")

size = os.path.getsize(TAR_LOCAL)
print(f"local tar: {TAR_LOCAL} ({size / 1024 / 1024:.1f} MB)")

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASS, timeout=20)


def run(cmd: str, timeout: int = 900) -> tuple[int, str]:
    print(f"\n$ {cmd}")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    sys.stdout.write(out)
    if err.strip():
        sys.stdout.write("[stderr] " + err)
    print(f"[exit {code}]")
    return code, out + err


try:
    # 1) 预检 + 装 Pillow（上传图片需要）
    code, _ = run(
        "cd /root/speakeasy && "
        "HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890 "
        ".venv/bin/python -m pip install -q pillow 2>&1 | tail -3; "
        ".venv/bin/python -c 'import PIL; print(\"Pillow\", PIL.__version__)'",
        timeout=300,
    )
    if code != 0:
        print("!! Pillow 安装失败，继续部署但图片上传可能不可用")

    # 2) 备份现有代码（可回滚）
    code, _ = run(
        f"cd /root/speakeasy && mkdir -p /root/deploy-backup && "
        f"tar -czf /root/deploy-backup/code-{STAMP}.tar.gz backend deploy 2>/dev/null; "
        f"du -sh /root/deploy-backup/code-{STAMP}.tar.gz"
    )
    if code != 0:
        raise SystemExit("代码备份失败，中止部署")

    # 3) 上传部署包
    print("\n== 上传部署包 ==")
    sftp = client.open_sftp()
    last = [-1]

    def cb(done, total):
        pct = int(done * 100 / max(total, 1))
        if pct >= last[0] + 10:
            last[0] = pct
            print(f"  {pct}% ({done / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MB)")

    sftp.put(TAR_LOCAL, REMOTE_TAR, callback=cb)
    sftp.close()
    print("上传完成")

    # 4) 解压覆盖（保留 data/.env/.venv/.git），旧静态产物挪走
    code, _ = run(
        f"cd {REMOTE} && "
        f"if [ -d backend/app/static/app ]; then mv backend/app/static/app backend/app/static/app.old-{STAMP}; fi && "
        f"tar -xzf {REMOTE_TAR} -C {REMOTE} && "
        f"echo '--- 解压完成 ---' && ls backend/app/static/app/index.html && "
        f"git rev-parse --short HEAD"
    )
    if code != 0:
        raise SystemExit("解压失败，未重启服务（旧代码仍可回滚）")

    # 5) 迁移数据库 + 重启服务 + 健康检查（用现成部署脚本）
    code, out = run(
        f"cd {REMOTE} && bash deploy/deploy_v3.sh 2>&1 | tail -40",
        timeout=1200,
    )
    if code != 0:
        print("\n!! deploy_v3.sh 失败，尝试回滚代码")
        run(
            f"cd /root && rm -rf speakeasy/backend speakeasy/deploy && "
            f"tar -xzf /root/deploy-backup/code-{STAMP}.tar.gz -C /root/speakeasy && "
            f"systemctl restart speakeasy && sleep 5 && systemctl is-active speakeasy"
        )
        raise SystemExit("部署失败，已回滚")

    # 6) 结果
    code, _ = run(
        "sleep 3; systemctl is-active speakeasy; "
        "curl -s -m 10 -o /dev/null -w 'health=%{http_code}\\n' http://127.0.0.1:8000/api/v3/health; "
        "curl -s -m 10 http://127.0.0.1:8000/api/v3/health | head -c 300; echo; "
        "curl -s -m 10 http://127.0.0.1:8000/ | grep -oE 'app/page-[a-z0-9]+\\.js' | head -1",
        timeout=120,
    )
    print("\n== 完成 ==")
    print("公网地址: http://47.104.24.236:8000/")
finally:
    client.close()
