#!/usr/bin/env python3
"""第二阶段 · 批次1: 官方规格采集 (20 款代表性机型)。

流程:
  1. 读取 data/seed/official_models.json
  2. 逐条抓取 apple_spec_url, 确认 HTTP 200 且页面包含 verify_marker
     (防止 SP 编号记错导致数据挂到错误来源上)
  3. 校验通过的写入 models 表 (upsert), 每条打印来源 URL 供人工抽查
  4. 有任何一条校验失败则退出码非 0, 失败条目不入库

用法:
    python3 scripts/collect_official.py            # 抓取校验 + 入库
    python3 scripts/collect_official.py --dry-run  # 只校验不入库
    python3 scripts/collect_official.py --offline  # 跳过抓取直接入库 (仅调试用)

零依赖, 仅标准库。
"""

import argparse
import gzip
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = ROOT / "data" / "seed" / "official_models.json"
DB_PATH = ROOT / "data" / "mac_upgrade.db"

USER_AGENT = "mac-upgrade-advisor/0.1 (spec verification; contact: local research tool)"
FETCH_DELAY_SECONDS = 1.0  # 对 Apple 服务器保持礼貌


def fetch(url):
    """返回 (final_url, html_text) 或抛异常。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return resp.geturl(), raw.decode("utf-8", errors="replace")


def verify_model(m):
    """抓取规格页并检查 verify_marker。返回 (ok, final_url, detail)。"""
    try:
        final_url, html = fetch(m["apple_spec_url"])
    except Exception as e:
        return False, m["apple_spec_url"], f"抓取失败: {e}"
    marker = m["verify_marker"].lower()
    if marker not in html.lower():
        # 提取 <title> 帮助定位实际抓到了什么页面
        title = ""
        lo = html.lower()
        if "<title>" in lo:
            s = lo.index("<title>") + 7
            title = html[s:s + 120].split("<")[0].strip()
        return False, final_url, f"页面不含标记 '{m['verify_marker']}' (实际页面标题: {title!r})"
    return True, final_url, "标记匹配"


def upsert_model(conn, m, final_url):
    conn.execute(
        """INSERT INTO models (model_name, model_identifier, release_year, family,
               cpu_model, cpu_socket, official_max_ram_gb, ram_type, ram_slots,
               storage_interface, stock_storage, nvme_bootable, max_macos, apple_spec_url,
               board_id, stock_gpu, stock_gpu_metal, oclp_caveat)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(model_identifier) DO UPDATE SET
               stock_storage=excluded.stock_storage,
               board_id=excluded.board_id,
               stock_gpu=excluded.stock_gpu,
               stock_gpu_metal=excluded.stock_gpu_metal,
               oclp_caveat=excluded.oclp_caveat,
               model_name=excluded.model_name,
               release_year=excluded.release_year,
               family=excluded.family,
               cpu_model=excluded.cpu_model,
               cpu_socket=excluded.cpu_socket,
               official_max_ram_gb=excluded.official_max_ram_gb,
               ram_type=excluded.ram_type,
               ram_slots=excluded.ram_slots,
               storage_interface=excluded.storage_interface,
               nvme_bootable=excluded.nvme_bootable,
               max_macos=excluded.max_macos,
               apple_spec_url=excluded.apple_spec_url,
               updated_at=datetime('now')""",
        (m["model_name"], m["model_identifier"], m["release_year"], m["family"],
         m["cpu_model"], m.get("cpu_socket"), m["official_max_ram_gb"],
         m.get("ram_type"), m["ram_slots"], m["storage_interface"],
         m.get("stock_storage"), m["nvme_bootable"], m.get("max_macos"), final_url,
         m.get("board_id"), m.get("stock_gpu"), m.get("stock_gpu_metal"),
         m.get("oclp_caveat")),
    )
    mid = conn.execute(
        "SELECT id FROM models WHERE model_identifier = ?", (m["model_identifier"],)
    ).fetchone()[0]
    conn.execute("DELETE FROM cpu_options WHERE model_id = ?", (mid,))
    for o in m.get("cpu_options", []):
        conn.execute(
            """INSERT INTO cpu_options (model_id, cpu_model, ghz, cores, config_type, notes)
               VALUES (?,?,?,?,?,?)""",
            (mid, o["cpu"], o["ghz"], o["cores"], o["config"], o.get("notes")),
        )
    conn.execute("DELETE FROM gpu_options WHERE model_id = ?", (mid,))
    for o in m.get("gpu_options", []):
        conn.execute(
            """INSERT INTO gpu_options (model_id, gpu_model, vram, config_type, notes)
               VALUES (?,?,?,?,?)""",
            (mid, o["gpu"], o.get("vram"), o["config"], o.get("notes")),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只校验不入库")
    parser.add_argument("--offline", action="store_true", help="跳过抓取直接入库 (调试用)")
    args = parser.parse_args()

    models = json.loads(SEED_PATH.read_text(encoding="utf-8"))["models"]
    print(f"种子机型: {len(models)} 款\n")

    conn = None
    if not args.dry_run:
        if not DB_PATH.exists():
            sys.exit(f"数据库不存在, 先运行 scripts/init_db.py: {DB_PATH}")
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys = ON")

    failures = []
    for i, m in enumerate(models, 1):
        ident = m["model_identifier"]
        if args.offline:
            ok, final_url, detail = True, m["apple_spec_url"], "offline 跳过校验"
        else:
            ok, final_url, detail = verify_model(m)
            time.sleep(FETCH_DELAY_SECONDS)
        status = "OK " if ok else "FAIL"
        print(f"[{i:2d}/{len(models)}] [{status}] {ident:18s} {m['model_name']}")
        print(f"          来源: {final_url}")
        if not ok:
            print(f"          原因: {detail}")
            failures.append((ident, detail))
            continue
        if conn is not None:
            upsert_model(conn, m, final_url)

    if conn is not None:
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
        conn.close()
        print(f"\n入库完成: models 表现有 {n} 条记录")

    if failures:
        print(f"\n校验失败 {len(failures)} 条 (未入库):")
        for ident, detail in failures:
            print(f"  - {ident}: {detail}")
        sys.exit(1)
    print("\n全部校验通过")


if __name__ == "__main__":
    main()
