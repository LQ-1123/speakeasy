"""服务器修复：上传补丁 → 合并备份中丢失的消息/动态 → 重启 → 验证。"""

import os
import sys
import time

import paramiko

HOST = os.getenv("SE_DEPLOY_HOST", "47.104.24.236")
USER = os.getenv("SE_DEPLOY_USER", "root")
PASS = os.getenv("SE_DEPLOY_PASS", "")
LOCAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REMOTE = "/root/speakeasy"

FILES = [
    "backend/app/v2/db.py",
    "backend/app/v2/router.py",
    "backend/app/v3/service.py",
    "deploy/deploy_v3.sh",
]

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASS, timeout=20)
sftp = client.open_sftp()

try:
    print("== 上传补丁 ==")
    for rel in FILES:
        local = os.path.join(LOCAL, rel)
        remote = f"{REMOTE}/{rel}"
        sftp.put(local, remote)
        print("  ", rel)
    sftp.close()

    def run(cmd, timeout=300):
        print(f"\n$ {cmd}")
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        sys.stdout.write(out)
        if err.strip():
            sys.stdout.write("[stderr] " + err)
        print(f"[exit {code}]")
        return code, out

    stamp = time.strftime("%Y%m%d-%H%M%S")
    merge = f'''
cd {REMOTE}
systemctl stop speakeasy
sleep 1
mkdir -p data/_backup/premerge-{stamp}
cp -a data/speakeasy_v2.db data/_backup/premerge-{stamp}/ 2>/dev/null || true
for s in -wal -shm; do [ -f data/speakeasy_v2.db$s ] && cp -a data/speakeasy_v2.db$s data/_backup/premerge-{stamp}/ || true; done
.venv/bin/python - <<'PY'
import sqlite3, os, glob
cur = sqlite3.connect("data/speakeasy_v2.db")
bak = sorted(glob.glob("data/_backup/deploy-*/speakeasy_v2.db"))[-1]
print("merge from:", bak)
cur.execute("ATTACH DATABASE ? AS bak", (bak,))
for t in ("messages", "moments"):
    cur.execute(f"INSERT OR IGNORE INTO {{t}} SELECT * FROM bak.{{t}}".format(t=t))
cur.commit()
for t in ("messages", "moments", "friends"):
    print(t, cur.execute(f"SELECT COUNT(*) FROM {{t}}".format(t=t)).fetchone()[0])
cur.close()
PY
systemctl start speakeasy
'''
    code, out = run(merge, timeout=600)
    if code != 0:
        raise SystemExit("合并/重启失败")

    print("\n== 等待服务启动 ==")
    ok = False
    for i in range(30):
        time.sleep(5)
        code, out = run("curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/v3/health", timeout=30)
        if out.strip().endswith("200"):
            print(f"  ready after {(i + 1) * 5}s")
            ok = True
            break
    if not ok:
        raise SystemExit("健康检查未通过")

    print("\n== 验证数据 ==")
    run("curl -s -m 30 http://127.0.0.1:8000/api/v2/bootstrap | head -c 600; echo")
    run("curl -s -m 30 http://127.0.0.1:8000/api/v3/feed/summary; echo")
    run("curl -s -m 30 'http://127.0.0.1:8000/api/v3/feed?limit=3' | head -c 200; echo")
finally:
    client.close()
