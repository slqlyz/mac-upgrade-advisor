#!/usr/bin/env python3
"""第二阶段 · 批次3: 平台层数据入库 (schema v2)。

内容: platforms (内存控制器上限) / expansion_ports (总线清单) /
hw_constraints (固件/系统约束) / gpu_arch_support (GPU 架构驱动区间) /
CPU 换装实证。

用法:
    python3 scripts/collect_platform.py            # 入库
    python3 scripts/collect_platform.py --verify   # 先探测来源 URL (404 阻断)
"""

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = ROOT / "data" / "seed" / "platform_layer.json"
DB_PATH = ROOT / "data" / "mac_upgrade.db"
USER_AGENT = "Mozilla/5.0 (compatible; mac-upgrade-advisor/0.1; source verification)"


def probe(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return f"HTTP {resp.status}", False
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}", e.code == 404
    except Exception as e:
        return f"无法访问 ({type(e).__name__})", False


def model_row(conn, ident):
    r = conn.execute(
        "SELECT id, apple_spec_url FROM models WHERE model_identifier = ?", (ident,)
    ).fetchone()
    if r is None:
        raise KeyError(f"机型未入库: {ident}")
    return r


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    if args.verify:
        urls = set()
        for p in seed["platforms"]:
            urls.add(p["controller_source_url"])
        for e in seed["expansion_ports"]:
            urls.update(x["source_url"] for x in e["ports"] if x["source_url"] != "$apple_spec")
        for c in seed["hw_constraints"] + seed["gpu_arch_support"] + seed["compatibility"]:
            urls.add(c["source_url"])
            urls.update(c.get("extra_source_urls") or [])
        print("来源 URL 探测:")
        hard = False
        for u in sorted(urls):
            status, is404 = probe(u)
            flag = "FAIL" if is404 else ("OK  " if status.startswith("HTTP 2") else "WARN")
            print(f"  [{flag}] {status:22s} {u}")
            hard = hard or is404
            time.sleep(0.4)
        if hard:
            sys.exit("存在 404 来源, 修正后重试")
        print()

    # 1. platforms + models.platform_id
    print("平台:")
    for p in seed["platforms"]:
        conn.execute(
            """INSERT INTO platforms (name, cpu_microarch, memory_controller,
                   controller_max_ram_gb, controller_source_url, notes)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                   cpu_microarch=excluded.cpu_microarch,
                   memory_controller=excluded.memory_controller,
                   controller_max_ram_gb=excluded.controller_max_ram_gb,
                   controller_source_url=excluded.controller_source_url,
                   notes=excluded.notes""",
            (p["name"], p["cpu_microarch"], p["memory_controller"],
             p["controller_max_ram_gb"], p["controller_source_url"], p.get("notes")),
        )
        pid = conn.execute("SELECT id FROM platforms WHERE name=?", (p["name"],)).fetchone()[0]
        for ident in p["models"]:
            conn.execute("UPDATE models SET platform_id=? WHERE model_identifier=?",
                         (pid, ident))
        print(f"  {p['name']:32s} 控制器上限 {str(p['controller_max_ram_gb']) + 'GB':>6s}  <- {', '.join(p['models'])}")
        print(f"      来源: {p['controller_source_url']}")

    # 2. expansion_ports (按机型全量替换)
    print("\n端口清单:")
    n_ports = 0
    for e in seed["expansion_ports"]:
        mid, spec_url = model_row(conn, e["model"])
        conn.execute("DELETE FROM expansion_ports WHERE model_id=?", (mid,))
        for x in e["ports"]:
            src = spec_url if x["source_url"] == "$apple_spec" else x["source_url"]
            conn.execute(
                """INSERT INTO expansion_ports (model_id, port_type, spec, count, notes, source_url)
                   VALUES (?,?,?,?,?,?)""",
                (mid, x["port_type"], x["spec"], x.get("count", 1), x.get("notes"), src),
            )
            n_ports += 1
        print(f"  {e['model']:18s} {len(e['ports'])} 类端口")

    # 3. hw_constraints
    print("\n约束:")
    for c in seed["hw_constraints"]:
        mid = pid = None
        if c["scope"] == "model":
            mid = model_row(conn, c["model"])[0]
        elif c["scope"] == "platform":
            pid = conn.execute("SELECT id FROM platforms WHERE name=?",
                               (c["platform"],)).fetchone()[0]
        exists = conn.execute(
            """SELECT 1 FROM hw_constraints WHERE scope=? AND model_id IS ?
               AND platform_id IS ? AND constraint_type=? AND source_url=?""",
            (c["scope"], mid, pid, c["constraint_type"], c["source_url"])).fetchone()
        if not exists:
            conn.execute(
                """INSERT INTO hw_constraints (scope, model_id, platform_id, constraint_type,
                       description, affected_versions, confidence_level, source_url,
                       extra_source_urls, corroboration_count, applicability)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (c["scope"], mid, pid, c["constraint_type"], c["description"],
                 c.get("affected_versions"), c["confidence_level"], c["source_url"],
                 json.dumps(c.get("extra_source_urls") or [], ensure_ascii=False),
                 c["corroboration_count"], c.get("applicability")))
        print(f"  [{c['confidence_level']:16s}] {c['scope']}/{c['constraint_type']}")
        print(f"      来源: {c['source_url']}")

    # 4. gpu_arch_support
    print("\nGPU 架构驱动区间:")
    for g in seed["gpu_arch_support"]:
        conn.execute(
            """INSERT INTO gpu_arch_support (vendor, arch, example_cards, macos_native,
                   macos_patched, notes, source_url, extra_source_urls, corroboration_count,
                   metal_support)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(vendor, arch) DO UPDATE SET
                   example_cards=excluded.example_cards,
                   macos_native=excluded.macos_native,
                   macos_patched=excluded.macos_patched,
                   notes=excluded.notes,
                   source_url=excluded.source_url,
                   extra_source_urls=excluded.extra_source_urls,
                   corroboration_count=excluded.corroboration_count,
                   metal_support=excluded.metal_support""",
            (g["vendor"], g["arch"], g.get("example_cards"), g["macos_native"],
             g.get("macos_patched"), g.get("notes"), g["source_url"],
             json.dumps(g.get("extra_source_urls") or [], ensure_ascii=False),
             g["corroboration_count"], g.get("metal_support")))
        print(f"  {g['vendor']:7s} {g['arch']:20s} 原生 {g['macos_native']}")

    # 5. CPU 换装等实证 (复用 compatibility)
    print("\n实证记录:")
    comp_ids = {}
    for c in seed["components"]:
        # 先查后插: UNIQUE 约束对 NULL 列不生效, 不能依赖 INSERT OR IGNORE
        found = conn.execute(
            """SELECT id FROM components WHERE category IS ? AND manufacturer IS ?
               AND part_model IS ? AND interface IS ? AND capacity_gb IS ?
               AND speed_spec IS ?""",
            (c["category"], c.get("manufacturer"), c.get("part_model"),
             c["interface"], c.get("capacity_gb"), c.get("speed_spec"))).fetchone()
        if found:
            comp_ids[c["key"]] = found[0]
        else:
            cur = conn.execute(
                """INSERT INTO components
                       (category, manufacturer, part_model, is_generic, interface,
                        capacity_gb, speed_spec, requires_adapter, notes)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (c["category"], c.get("manufacturer"), c.get("part_model"),
                 c.get("is_generic", 0), c["interface"], c.get("capacity_gb"),
                 c.get("speed_spec"), c.get("requires_adapter", 0), c.get("notes")))
            comp_ids[c["key"]] = cur.lastrowid
    comp_by_key = {c["key"]: c for c in seed["components"]}
    for row in seed["compatibility"]:
        comp = comp_by_key[row["component_key"]]
        cid = comp_ids[row["component_key"]]
        mid = model_row(conn, row["model_identifier"])[0]
        conn.execute(
            """INSERT INTO compatibility
                   (model_id, component_id, confidence_level, source_url, source_type,
                    corroboration_count, extra_source_urls, verified_macos_versions,
                    result, max_working_capacity_gb, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(model_id, component_id, confidence_level, source_url)
               DO UPDATE SET notes=excluded.notes,
                             corroboration_count=excluded.corroboration_count,
                             extra_source_urls=excluded.extra_source_urls""",
            (mid, cid, row["confidence_level"], row["source_url"], row["source_type"],
             row["corroboration_count"],
             json.dumps(row.get("extra_source_urls") or [], ensure_ascii=False),
             row.get("verified_macos_versions"), row.get("result", "works"),
             row.get("max_working_capacity_gb"), row.get("notes")))
        print(f"  [{row['confidence_level']}] {row['model_identifier']} × {comp['part_model']}")
        print(f"      来源: {row['source_url']}")

    conn.commit()
    stats = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("platforms", "expansion_ports", "hw_constraints", "gpu_arch_support")}
    conn.close()
    print("\n当前库内: " + ", ".join(f"{t}={n}" for t, n in stats.items()))


if __name__ == "__main__":
    main()
