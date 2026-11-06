"""把最新前端静态构建上传到服务器并替换（无需重启后端）。"""

import os
import sys
import tarfile
import tempfile
import time

import paramiko

HOST = os.getenv("SE_DEPLOY_HOST", "47.104.24.236")
USER = os.getenv("SE_DEPLOY_USER", "root")
PASS = os.getenv("SE_DEPLOY_PASS", "")
LOCAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(LOCAL, "backend", "app", "static", "app")
REMOTE = "/root/speakeasy"
STAMP = time.strftime("%Y%m%d-%H%M%S")

if not os.path.isdir(STATIC):
    raise SystemExit("缺少静态构建目录")

# 打包（仅 static app）
tar_path = os.path.join(tempfile.gettempdir(), f"speakeasy-static-{STAMP}.tar.gz")
with tarfile.open(tar_path, "w:gz") as tar:
    tar.add(STATIC, arcname="app")
size = os.path.getsize(tar_path)
print(f"static tar: {tar_path} ({size/1024/1024:.1f} MB)")

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASS, timeout=20)
try:
    sftp = client.open_sftp()
    remote_tar = "/root/speakeasy-static.tar.gz"
    last = [-1]

    def cb(done, total):
        pct = int(done * 100 / max(total, 1))
        if pct >= last[0] + 20:
            last[0] = pct
            print(f"  {pct}%")

    sftp.put(tar_path, remote_tar, callback=cb)
    sftp.close()
    print("上传完成")

    script = f'''
cd {REMOTE}
if [ -d backend/app/static/app ]; then mv backend/app/static/app backend/app/static/app.old-{STAMP}; fi
tar -xzf /root/speakeasy-static.tar.gz -C backend/app/static
ls backend/app/static/app/index.html
grep -oE 'app/page-[a-z0-9]+\\.js' backend/app/static/app/index.html | head -1
'''
    stdin, stdout, stderr = client.exec_command(script, timeout=300)
    print(stdout.read().decode("utf-8", "replace"))
    err = stderr.read().decode("utf-8", "replace")
    if err.strip():
        print("[stderr]", err)
    print("exit", stdout.channel.recv_exit_status())
finally:
    client.close()
