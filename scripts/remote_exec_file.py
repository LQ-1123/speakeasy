"""把本地脚本上传到服务器并执行（避免 SSH 命令行的引号地狱）。

用法：
    python scripts/remote_exec_file.py <local_script.py> [--python /root/speakeasy/.venv/bin/python]

密码从环境变量 SE_DEPLOY_PASS 读取（与 server_ssh.py 一致），绝不落盘/打印。
"""

from __future__ import annotations

import os
import stat
import sys

import paramiko

HOST = os.getenv("SE_DEPLOY_HOST", "47.104.24.236")
USER = os.getenv("SE_DEPLOY_USER", "root")
PASS = os.getenv("SE_DEPLOY_PASS", "")
PORT = int(os.getenv("SE_DEPLOY_PORT", "22"))
REMOTE_PATH = "/tmp/se_remote_task.py"


def main() -> int:
    args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    interpreter = "/root/speakeasy/.venv/bin/python"
    for index, arg in enumerate(sys.argv[1:]):
        if arg == "--python":
            interpreter = sys.argv[index + 2]
    if not args:
        raise SystemExit("用法: remote_exec_file.py <local_script.py> [--python <远端python>]")
    if not PASS:
        raise SystemExit("缺少 SE_DEPLOY_PASS")
    local_path = args[0]
    with open(local_path, "rb") as fh:
        payload = fh.read()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=PORT, username=USER, password=PASS, timeout=20, banner_timeout=20)
    try:
        sftp = client.open_sftp()
        with sftp.open(REMOTE_PATH, "wb") as remote:
            remote.write(payload)
        sftp.chmod(REMOTE_PATH, stat.S_IRUSR | stat.S_IWUSR)
        sftp.close()
        command = f"cd /root/speakeasy && {interpreter} {REMOTE_PATH} 2>&1"
        stdin, stdout, stderr = client.exec_command(command, timeout=600)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        print(out)
        if err.strip():
            print("[stderr]", err)
        print(f"[exit {code}]")
        return code
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
