"""APK 静态检查：生产 API 地址、测试地址残留、密钥泄漏、版本信息（只读 zip）。"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    apk = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "deliverables" / "SpeakEasy-v4.2.0-release.apk"
    if not apk.is_file():
        raise SystemExit(f"APK 不存在: {apk}")
    patterns = {
        "prod_domain": re.compile(r"47-104-24-236\.sslip\.io"),
        "prod_ip": re.compile(r"47\.104\.24\.236"),
        "localhost": re.compile(r"127\.0\.0\.1(?::8000)?"),
        "localhost_name": re.compile(r"localhost(?::8000)?"),
        "lan_ip": re.compile(r"192\.168\.\d+\.\d+"),
        "secret_sk": re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
        "secret_key_assign": re.compile(r"(?i)(api[_-]?key|secret|password)\s*[:=]\s*[\"'][^\"']{16,}[\"']"),
    }
    hits: dict[str, list[str]] = {name: [] for name in patterns}
    text_entries = 0
    entries = 0
    with zipfile.ZipFile(apk) as archive:
        names = archive.namelist()
        entries = len(names)
        for name in names:
            if not name.lower().endswith((".js", ".html", ".json", ".txt", ".xml", ".css")):
                continue
            try:
                raw = archive.read(name)
            except KeyError:
                continue
            if len(raw) > 8 * 1024 * 1024:
                continue
            try:
                text = raw.decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                continue
            text_entries += 1
            for key, pattern in patterns.items():
                for match in pattern.finditer(text):
                    snippet = text[max(0, match.start() - 40): match.end() + 40].replace("\n", " ")
                    hits[key].append(f"{name}: {snippet[:180]}")
    report = {
        "apk": str(apk),
        "size_bytes": apk.stat().st_size,
        "zip_entries": entries,
        "text_entries_scanned": text_entries,
        "hits": {key: value[:10] for key, value in hits.items()},
        "hit_counts": {key: len(value) for key, value in hits.items()},
        "prod_domain_present": bool(hits["prod_domain"]),
        # 只把「带端口的 127.0.0.1」当成本地地址残留；把 localhost 字面量（polyfills /
        # offline 重载页里的通用文案）记为提示，不作为失败。
        "localhost_present": bool(hits["localhost"]),
        "localhost_word_only": bool(hits["localhost_name"]) and not bool(hits["localhost"]),
        "lan_ip_present": bool(hits["lan_ip"]),
        "secret_leak_present": bool(hits["secret_sk"] or hits["secret_key_assign"]),
    }
    out = ROOT / "artifacts" / "qa-reports" / "apk-inspect.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = (
        report["prod_domain_present"]
        and not report["localhost_present"]
        and not report["lan_ip_present"]
        and not report["secret_leak_present"]
    )
    print("APK_STATIC_CHECK:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
