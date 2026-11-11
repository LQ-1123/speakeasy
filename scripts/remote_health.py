"""部署后服务端健康巡检（只读）：服务状态、近期日志中的 5xx/异常、关键数据一致性。"""

from __future__ import annotations

import json
import re
import subprocess


def run(command: str, timeout: int = 60) -> str:
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
        return (result.stdout or "") + (result.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return f"err:{type(exc).__name__}: {exc}"


def main() -> int:
    payload: dict[str, object] = {}
    payload["service"] = run("systemctl is-active speakeasy").strip()
    payload["uptime"] = run("systemctl show speakeasy -p ActiveEnterTimestamp --value").strip()
    logs = run("journalctl -u speakeasy --since '-15 min' --no-pager -o cat", timeout=120)
    payload["log_lines_15m"] = len(logs.splitlines())
    payload["http_5xx_lines"] = [
        line for line in logs.splitlines() if re.search(r'" 5\d\d ', line)
    ][:20]
    payload["traceback_lines"] = [
        line for line in logs.splitlines() if "Traceback (most recent call last)" in line
    ][:10]
    payload["error_lines"] = [
        line for line in logs.splitlines() if re.search(r"\bERROR\b", line)
    ][:20]
    payload["heap"] = run("free -m | head -2")
    payload["disk"] = run("df -h / | tail -1")
    payload["recent_http_5xx_total"] = len(payload["http_5xx_lines"])
    payload["recent_traceback_total"] = len(payload["traceback_lines"])
    print("SERVER_HEALTH " + json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
