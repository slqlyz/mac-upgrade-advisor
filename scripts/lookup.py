#!/usr/bin/env python3
"""按机型查询升级数据 (测试/抽查用的展示层, 非推荐引擎)。

用法:
    python3 scripts/lookup.py --list              # 列出库内全部机型
    python3 scripts/lookup.py MacBookPro11,4      # 按机型标识精确查
    python3 scripts/lookup.py "mac mini 2012"     # 按名称模糊查
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "mac_upgrade.db"

LAYER_LABEL = {
    "official": "官方支持",
    "community_tested": "社区验证 (≥2 独立来源)",
    "experimental": "实验性 (孤例, 风险自担)",
}
LAYER_ORDER = {"official": 0, "community_tested": 1, "experimental": 2}
PORT_LABEL = {
    "pcie_slot": "PCIe 插槽", "thunderbolt": "雷电", "sata": "SATA 位",
    "sodimm_slot": "内存插槽", "mxm": "MXM 显卡位", "apple_ssd_blade": "专有 SSD 刀片槽",
    "usb": "USB", "optical_bay": "光驱位", "firewire": "FireWire", "sd_card": "SD 卡槽",
}
CONSTRAINT_LABEL = {
    "cpu_firmware_check": "CPU 固件校验", "nvme_boot": "NVMe 引导",
    "egpu_support": "eGPU 支持", "gpu_driver": "GPU 驱动",
    "sleep_quirk": "睡眠缺陷", "bandwidth_share": "带宽共享", "other": "其他",
}
SEVERITY_LABEL = {
    "no_boot": "无法启动", "instability": "不稳定",
    "performance_degradation": "性能下降", "feature_loss": "功能损失", "cosmetic": "轻微",
}
CAT_LABEL = {
    "ram": "内存", "ssd": "固态硬盘", "hdd": "机械硬盘", "wifi_bt_card": "无线网卡",
    "gpu": "显卡", "cpu": "处理器", "optical_bay_caddy": "光驱位托架",
    "adapter": "转接卡", "battery": "电池", "display": "屏幕", "other": "其他",
}
NVME_LABEL = {
    "native": "原生支持", "firmware_update_required": "需固件更新 (随 10.13+ 自动完成)",
    "opencore_required": "需 OpenCore", "no": "不支持",
}
RESULT_LABEL = {
    "works": "可用",
    "works_with_caveats": "可用但有注意事项",
    "partial": "部分可用",
    "failed": "失败案例",
}


def connect():
    if not DB_PATH.exists():
        sys.exit(f"数据库不存在, 先运行 scripts/init_db.py 和采集脚本: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def list_models(conn):
    rows = conn.execute(
        """SELECT m.model_identifier, m.model_name, m.release_year,
                  COUNT(c.id) AS n_compat
           FROM models m LEFT JOIN compatibility c ON c.model_id = m.id
           GROUP BY m.id ORDER BY m.family, m.release_year"""
    ).fetchall()
    print(f"库内机型 {len(rows)} 款 (末列为已收录升级条目数):\n")
    for r in rows:
        print(f"  {r['model_identifier']:18s} {r['model_name']:55s} [{r['n_compat']}]")


def find_model(conn, query):
    row = conn.execute(
        "SELECT * FROM models WHERE model_identifier = ? COLLATE NOCASE", (query,)
    ).fetchone()
    if row:
        return row
    like = f"%{'%'.join(query.split())}%"
    rows = conn.execute(
        "SELECT * FROM models WHERE model_name LIKE ? COLLATE NOCASE ORDER BY release_year",
        (like,),
    ).fetchall()
    if not rows:
        sys.exit(f"未找到机型: {query!r} (用 --list 查看库内机型)")
    if len(rows) > 1:
        print(f"匹配到 {len(rows)} 款, 请用机型标识精确查询:")
        for r in rows:
            print(f"  {r['model_identifier']:18s} {r['model_name']}")
        sys.exit(1)
    return rows[0]


def component_label(r):
    if r["is_generic"]:
        base = f"(泛型) {r['interface']}"
    else:
        base = " ".join(x for x in (r["manufacturer"], r["part_model"]) if x)
    extra = []
    if r["capacity_gb"]:
        extra.append(f"{r['capacity_gb']}GB")
    if r["speed_spec"]:
        extra.append(r["speed_spec"])
    return base + (f" [{', '.join(extra)}]" if extra else "")


def show_model(conn, m):
    print(f"═══ {m['model_name']}  ({m['model_identifier']}) ═══\n")
    plat = None
    if m["platform_id"]:
        plat = conn.execute("SELECT * FROM platforms WHERE id = ?",
                            (m["platform_id"],)).fetchone()
    print("官方规格 (来源: Apple):")
    ctrl = ""
    if plat and plat["controller_max_ram_gb"] and m["ram_slots"] > 0:
        mod = plat["max_module_gb"]
        phys = min(plat["controller_max_ram_gb"], m["ram_slots"] * mod) if mod else plat["controller_max_ram_gb"]
        detail = f"{m['ram_slots']}槽×单条{mod}GB, 控制器 {plat['controller_max_ram_gb']}GB" if mod \
            else f"控制器 {plat['controller_max_ram_gb']}GB"
        vary = " ⚠随 CPU SKU 而异, 见平台备注" if phys < m["official_max_ram_gb"] else ""
        ctrl = f" / 物理可达 {phys}GB ({detail}{vary}; 平台: {plat['name']}, 来源 Intel ARK)"
    if m["ram_slots"] > 0:
        ram_upg = "插槽式, 可自行升级"
    elif 2012 <= m["release_year"] <= 2019:
        _f = max(16, m["official_max_ram_gb"])
        ctrl = f" / 物理可达 {_f}GB (颗粒加焊, 见野路子; 上限受主板颗粒位布线约束)"
        ram_upg = "焊接, 常规不可升级"
    else:
        ram_upg = "焊接, 常规不可升级 (2012 前部分主板缺高容量数据线, 加焊亦不通用)"
    print(f"  内存: 官方上限 {m['official_max_ram_gb']}GB{ctrl}, {m['ram_type']}, 插槽 x{m['ram_slots']} — {ram_upg}")
    if plat and plat["notes"] and m["ram_slots"] > 0:
        print(f"    平台备注: {plat['notes']}")
    print(f"  存储接口: {m['storage_interface']}  |  NVMe 引导: {NVME_LABEL.get(m['nvme_bootable'], m['nvme_bootable'])}")
    if m["stock_gpu"]:
        gm = "Metal ✓" if m["stock_gpu_metal"] else "非 Metal (OCLP 新系统体验受损)"
        print(f"  原装显卡: {m['stock_gpu']} — {gm}")
    sock = m['cpu_socket'] or '未知'
    upg = ("常规不可升级 (板级改装属专业路径, 见约束)" if sock.startswith("BGA")
           else "插槽式, CPU 可物理换装 (同代固件约束见下)" if sock.startswith("LGA")
           else "未知")
    print(f"  CPU 插槽: {sock} — {upg}  |  官方最高系统: {m['max_macos'] or '未知'}")
    if m["board_id"]:
        print(f"  逻辑板 ID: {m['board_id']}  (OCLP/黑苹果场景用, 来源: OCLP smbios 数据)")
    print(f"  来源: {m['apple_spec_url']}\n")

    cpus = conn.execute(
        """SELECT * FROM cpu_options WHERE model_id = ?
           ORDER BY config_type = 'configurable', cores, ghz""",
        (m["id"],),
    ).fetchall()
    if cpus:
        print(f"  CPU 配置档 ({len(cpus)} 档; 主频/核数源自 Apple 页面, 型号编号经 EveryMac 核对):")
        for c in cpus:
            tag = "标配" if c["config_type"] == "standard" else "选配"
            note = f", {c['notes']}" if c["notes"] else ""
            print(f"    [{tag}] {c['ghz']}GHz {c['cores']}核 — {c['cpu_model']}{note}")
        print()

    gpus = conn.execute(
        """SELECT * FROM gpu_options WHERE model_id = ?
           ORDER BY config_type = 'configurable', id""",
        (m["id"],),
    ).fetchall()
    if gpus:
        print(f"  显卡配置档 ({len(gpus)} 档; 源自 Apple 规格页):")
        for g in gpus:
            tag = "标配" if g["config_type"] == "standard" else "选配"
            vram = f" {g['vram']}" if g["vram"] else ""
            note = f", {g['notes']}" if g["notes"] else ""
            print(f"    [{tag}] {g['gpu_model']}{vram}{note}")
        print()

    ports = conn.execute(
        "SELECT * FROM expansion_ports WHERE model_id = ? ORDER BY port_type",
        (m["id"],),
    ).fetchall()
    if ports:
        print(f"  扩展端口/总线 ({len(ports)} 条):")
        for p in ports:
            cnt = f" x{p['count']}" if p["count"] > 1 else ""
            note = f"  ({p['notes']})" if p["notes"] else ""
            print(f"    [{PORT_LABEL.get(p['port_type'], p['port_type'])}] {p['spec']}{cnt}{note}")
        print()

    cons = conn.execute(
        """SELECT * FROM hw_constraints
           WHERE (scope='model' AND model_id=?) OR (scope='platform' AND platform_id=?)
              OR scope='global'
           ORDER BY scope, constraint_type""",
        (m["id"], m["platform_id"] or -1),
    ).fetchall()
    tb_specs = [p["spec"] for p in ports if p["port_type"] == "thunderbolt"]
    ctx = {
        "has_tb3": any("Thunderbolt 3" in x for x in tb_specs),
        "has_tb1_or_tb2": any(("Thunderbolt 1" in x or "Thunderbolt 2" in x) for x in tb_specs),
        "bga_cpu_or_soldered_ram": (m["cpu_socket"] or "").startswith("BGA") or m["ram_slots"] == 0,
    }

    gpu_paths = []
    if any(p["port_type"] == "mxm" for p in ports):
        gpu_paths.append("MXM 显卡位")
    if any(p["port_type"] == "pcie_slot" for p in ports):
        gpu_paths.append("PCIe 插槽")
    if ctx["has_tb3"]:
        gpu_paths.append("eGPU (TB3, 官方支持)")
    elif ctx["has_tb1_or_tb2"]:
        gpu_paths.append("eGPU (TB1/2, 非官方需社区脚本)")
    if gpu_paths:
        print(f"  显卡升级路径: {' / '.join(gpu_paths)}  (架构×驱动区间见 gpu_arch_support 表)")
        print()

    cons = [c for c in cons
            if not c["applicability"] or ctx.get(c["applicability"], True)]
    if cons:
        print(f"  固件/系统约束 ({len(cons)} 条):")
        for c in cons:
            scope = {"global": "全系统", "platform": "整个平台", "model": "本机型"}[c["scope"]]
            print(f"    [{LAYER_LABEL[c['confidence_level']].split(' ')[0]}/{scope}] {CONSTRAINT_LABEL.get(c['constraint_type'], c['constraint_type'])}: {c['description']}")
            if c["affected_versions"]:
                print(f"        影响范围: {c['affected_versions']}")
            print(f"        来源: {c['source_url']}")
        print()

    rows = conn.execute(
        """SELECT c.*, co.manufacturer, co.part_model, co.is_generic, co.interface,
                  co.capacity_gb, co.speed_spec, co.category, co.requires_adapter
           FROM compatibility c JOIN components co ON co.id = c.component_id
           WHERE c.model_id = ?""",
        (m["id"],),
    ).fetchall()
    rows.sort(key=lambda r: LAYER_ORDER[r["confidence_level"]])

    if not rows:
        print("升级方案: 暂未收录\n")
    else:
        print(f"升级方案 ({len(rows)} 条, 按可信度分层):\n")
        current = None
        for r in rows:
            if r["confidence_level"] != current:
                current = r["confidence_level"]
                print(f"  ── {LAYER_LABEL[current]} ──")
            print(f"  • [{CAT_LABEL.get(r['category'], r['category'])}] {component_label(r)} — {RESULT_LABEL[r['result']]}")
            if r["max_working_capacity_gb"]:
                print(f"      实测容量上限: {r['max_working_capacity_gb']}GB")
            if r["verified_macos_versions"]:
                print(f"      验证系统版本: {r['verified_macos_versions']}")
            if r["requires_adapter"]:
                print(f"      需要转接卡")
            if r["notes"]:
                print(f"      注意: {r['notes']}")
            print(f"      来源: {r['source_url']}")
            for url in json.loads(r["extra_source_urls"] or "[]"):
                print(f"      来源: {url}")
            print()

    conflicts = conn.execute(
        """SELECT k.*, a.manufacturer AS am, a.part_model AS ap, a.interface AS ai,
                  a.is_generic AS ag
           FROM known_conflicts k JOIN components a ON a.id = k.component_a_id
           WHERE k.model_id = ?""",
        (m["id"],),
    ).fetchall()
    if conflicts:
        print(f"⚠ 已知冲突/风险案例 ({len(conflicts)} 条):\n")
        for k in conflicts:
            comp = f"(泛型) {k['ai']}" if k["ag"] else " ".join(x for x in (k["am"], k["ap"]) if x)
            print(f"  • {comp}  [严重度: {SEVERITY_LABEL.get(k['severity'], k['severity'])}]")
            print(f"      {k['description']}")
            if k["workaround"]:
                print(f"      规避: {k['workaround']}")
            print(f"      来源: {k['source_url']}")
            for url in json.loads(k["extra_source_urls"] or "[]"):
                print(f"      来源: {url}")
            print()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="?", help="机型标识或名称关键词")
    parser.add_argument("--list", action="store_true", help="列出库内全部机型")
    args = parser.parse_args()

    conn = connect()
    if args.list or not args.query:
        list_models(conn)
    else:
        show_model(conn, find_model(conn, args.query))
    conn.close()


if __name__ == "__main__":
    main()
