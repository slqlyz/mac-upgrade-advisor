#!/usr/bin/env python3
"""逐字段审计: 抓取每个机型的 Apple 规格页, 提取处理器/内存/存储原文,
与库内数值并排输出, 供逐条比对修正。

用法:
    python3 scripts/audit_official.py                 # 审计全部
    python3 scripts/audit_official.py MacBookPro9,2   # 只审计指定机型
"""

import gzip
import html
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "mac_upgrade.db"
USER_AGENT = "mac-upgrade-advisor/0.1 (spec field audit)"


def fetch_text(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    page = raw.decode("utf-8", errors="replace")
    page = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    page = re.sub(r"<[^>]+>", "\n", page)
    page = html.unescape(page)
    lines = [ln.strip() for ln in page.splitlines() if ln.strip()]
    return lines


def section(lines, *headings, take=8, maxchars=400):
    """返回标题行之后的若干行原文 (Apple 规格页每节是 标题 + 条目列表)。"""
    out = []
    for h in headings:
        for i, ln in enumerate(lines):
            if ln.lower() == h.lower():
                chunk = []
                for nxt in lines[i + 1:i + 1 + take]:
                    if re.fullmatch(r"[A-Z][A-Za-z /&-]{2,30}", nxt) and len(chunk) >= 1:
                        break  # 撞到下一个节标题
                    chunk.append(nxt)
                out.append(" | ".join(chunk)[:maxchars])
                break
    return out


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM models ORDER BY family, release_year"
    rows = [r for r in conn.execute(sql)
            if only is None or r["model_identifier"].lower() == only.lower()]
    conn.close()

    for m in rows:
        print("=" * 78)
        print(f"{m['model_identifier']}  {m['model_name']}")
        print(f"  页面: {m['apple_spec_url']}")
        try:
            lines = fetch_text(m["apple_spec_url"])
        except Exception as e:
            print(f"  !! 抓取失败: {e}")
            continue
        print(f"  [库] CPU: {m['cpu_model']}")
        for s in section(lines, "Processor", "Processor and memory", take=10):
            print(f"  [页] Processor: {s}")
        print(f"  [库] 内存: 上限{m['official_max_ram_gb']}GB, {m['ram_type']}, 插槽x{m['ram_slots']}")
        for s in section(lines, "Memory", take=8):
            print(f"  [页] Memory: {s}")
        print(f"  [库] 存储接口: {m['storage_interface']}")
        for s in section(lines, "Storage", "Storage1", take=8):
            print(f"  [页] Storage: {s}")
        time.sleep(1.0)


if __name__ == "__main__":
    main()
