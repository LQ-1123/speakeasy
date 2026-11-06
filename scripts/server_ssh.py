"""通过 SSH 在云服务器上执行诊断命令（密码从环境变量读取，不落盘）。"""

import os
import sys

import paramiko

HOST = os.getenv("SE_DEPLOY_HOST", "47.104.24.236")
USER = os.getenv("SE_DEPLOY_USER", "root")
PASS = os.getenv("SE_DEPLOY_PASS", "")
PORT = int(os.getenv("SE_DEPLOY_PORT", "22"))

if not PASS:
    raise SystemExit("缺少 SE_DEPLOY_PASS")

cmd = sys.argv[1] if len(sys.argv) > 1 else "hostname; whoami; uptime"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, port=PORT, username=USER, password=PASS, timeout=20, banner_timeout=20)
try:
    stdin, stdout, stderr = client.exec_command(cmd, timeout=120)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    print(out)
    if err.strip():
        print("[stderr]", err)
    print("[exit]", code)
finally:
    client.close()
