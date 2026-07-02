#!/usr/bin/env python3
"""第二阶段 · 批次2: 社区方案试点采集 (3 款热门机型)。

流程:
  1. 读取 data/seed/community_pilot.json
  2. --verify 时逐一探测所有来源 URL (404 视为失败; 403/超时等反爬信号
     记为 "未能自动验证", 保留供人工检查, 不阻断)
  3. 组件 upsert -> 兼容关系/冲突案例入库, 每条打印可信度 + 全部来源 URL
  4. 可信度分层规则由数据库触发器强制 (official 来源类型限制、
     community_tested 需 >= 2 来源), 触发器拒绝的条目会报错并计入失败

用法:
    python3 scripts/collect_community.py             # 入库
    python3 scripts/collect_community.py --verify    # 先探测来源 URL 再入库
"""

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = ROOT / "data" / "seed" / "community_pilot.json"
DB_PATH = ROOT / "data" / "mac_upgrade.db"
USER_AGENT = "Mozilla/5.0 (compatible; mac-upgrade-advisor/0.1; source verification)"


def probe(url):
    """返回 (状态描述, 是否硬失败)。404 = 硬失败; 反爬/超时 = 软警告。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return f"HTTP {resp.status}", False
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}", e.code == 404
    except Exception as e:
        return f"无法访问 ({type(e).__name__})", False


def collect_urls(seed):
    urls = set()
    for section in ("compatibility", "known_conflicts"):
        for row in seed[section]:
            urls.add(row["source_url"])
            urls.update(row.get("extra_source_urls") or [])
    return sorted(urls)


def upsert_component(conn, c):
    # 先查后插: UNIQUE 约束对 NULL 列不生效 (NULL != NULL), 不能依赖 INSERT OR IGNORE
    row = conn.execute(
        """SELECT id FROM components
           WHERE category IS ? AND manufacturer IS ? AND part_model IS ?
             AND interface IS ? AND capacity_gb IS ? AND speed_spec IS ?""",
        (c["category"], c.get("manufacturer"), c.get("part_model"),
         c["interface"], c.get("capacity_gb"), c.get("speed_spec")),
    ).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        """INSERT INTO components
               (category, manufacturer, part_model, is_generic, interface,
                capacity_gb, speed_spec, requires_adapter, notes)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (c["category"], c.get("manufacturer"), c.get("part_model"),
         c.get("is_generic", 0), c["interface"], c.get("capacity_gb"),
         c.get("speed_spec"), c.get("requires_adapter", 0), c.get("notes")),
    )
    return cur.lastrowid


def model_id(conn, identifier):
    row = conn.execute(
        "SELECT id FROM models WHERE model_identifier = ?", (identifier,)
    ).fetchone()
    if row is None:
        raise KeyError(f"机型未入库 (先运行 collect_official.py): {identifier}")
    return row[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="入库前探测所有来源 URL")
    args = parser.parse_args()

    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    if args.verify:
        print("来源 URL 探测:")
        hard_fail = False
        for url in collect_urls(seed):
            status, is404 = probe(url)
            flag = "FAIL" if is404 else ("OK  " if status.startswith("HTTP 2") else "WARN")
            print(f"  [{flag}] {status:24s} {url}")
            hard_fail = hard_fail or is404
            time.sleep(0.5)
        if hard_fail:
            sys.exit("存在 404 来源, 请修正种子数据后重试")
        print()

    comp_ids = {c["key"]: upsert_component(conn, c) for c in seed["components"]}
    print(f"组件入库: {len(comp_ids)} 个\n")

    errors = 0
    print("兼容关系:")
    for row in seed["compatibility"]:
        label = f"{row['model_identifier']} × {row['component_key']}"
        try:
            conn.execute(
                """INSERT INTO compatibility
                       (model_id, component_id, confidence_level, source_url,
                        source_type, corroboration_count, extra_source_urls,
                        verified_macos_versions, result, max_working_capacity_gb, notes,
                        path_class)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(model_id, component_id, confidence_level, source_url)
                   DO UPDATE SET
                       corroboration_count=excluded.corroboration_count,
                       extra_source_urls=excluded.extra_source_urls,
                       verified_macos_versions=excluded.verified_macos_versions,
                       result=excluded.result,
                       max_working_capacity_gb=excluded.max_working_capacity_gb,
                       notes=excluded.notes,
                       path_class=excluded.path_class""",
                (model_id(conn, row["model_identifier"]), comp_ids[row["component_key"]],
                 row["confidence_level"], row["source_url"], row["source_type"],
                 row["corroboration_count"],
                 json.dumps(row.get("extra_source_urls") or [], ensure_ascii=False),
                 row.get("verified_macos_versions"), row.get("result", "works"),
                 row.get("max_working_capacity_gb"), row.get("notes"),
                 row.get("path_class", "standard")),
            )
            print(f"  [{row['confidence_level']:16s}] {label}")
            for url in [row["source_url"]] + (row.get("extra_source_urls") or []):
                print(f"      来源: {url}")
        except (sqlite3.IntegrityError, KeyError) as e:
            errors += 1
            print(f"  [拒绝] {label}: {e}")

    print("\n冲突案例:")
    for row in seed["known_conflicts"]:
        label = f"{row['model_identifier']} × {row['component_a_key']}"
        try:
            mid = model_id(conn, row["model_identifier"])
            ca = comp_ids[row["component_a_key"]]
            cb = comp_ids[row["component_b_key"]] if row.get("component_b_key") else None
            exists = conn.execute(
                """SELECT 1 FROM known_conflicts
                   WHERE model_id=? AND component_a_id=? AND source_url=?""",
                (mid, ca, row["source_url"]),
            ).fetchone()
            if not exists:
                conn.execute(
                    """INSERT INTO known_conflicts
                           (model_id, component_a_id, component_b_id, severity,
                            description, workaround, affected_macos_versions,
                            source_url, corroboration_count, extra_source_urls)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (mid, ca, cb, row["severity"], row["description"],
                     row.get("workaround"), row.get("affected_macos_versions"),
                     row["source_url"], row["corroboration_count"],
                     json.dumps(row.get("extra_source_urls") or [], ensure_ascii=False)),
                )
            print(f"  [{row['severity']:24s}] {label}")
            for url in [row["source_url"]] + (row.get("extra_source_urls") or []):
                print(f"      来源: {url}")
        except (sqlite3.IntegrityError, KeyError) as e:
            errors += 1
            print(f"  [拒绝] {label}: {e}")

    conn.commit()
    stats = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("models", "components", "compatibility", "known_conflicts")}
    conn.close()
    print(f"\n当前库内: " + ", ".join(f"{t}={n}" for t, n in stats.items()))
    if errors:
        sys.exit(f"{errors} 条被拒绝, 见上方输出")


if __name__ == "__main__":
    main()
