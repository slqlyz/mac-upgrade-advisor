#!/usr/bin/env python3
"""数据一致性对账: 找出"手动标注但可由底层数据推导"的字段, 推导值与存储值比对。

原则: 可推导的字段要么删掉改为运行时计算, 要么保留但必须通过本脚本对账 —
不允许存在"手标了但没人校验"的第三种状态。

检查项:
  1. family        ← model_name 前缀解析
  2. cpu_model     ← cpu_options 基础款 (标配档第一条)
  3. storage_interface ← expansion_ports 推导 (SATA位/刀片类型/PCIe槽)
  4. nvme_bootable ← 规则: 刀片 NVMe→native; 刀片 AHCI(2013+)→固件更新;
                     SATA/焊接→no; PCIe 槽机型→固件更新 (BootROM)
  5. cpu_socket    ← 同平台内应当一致 (混装则该字段不能上移到平台层)
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "mac_upgrade.db"

FAMILIES = ["MacBook Pro", "MacBook Air", "MacBook", "iMac Pro", "iMac",
            "Mac mini", "Mac Pro", "Xserve"]


def derive_family(name):
    for f in FAMILIES:
        if name.startswith(f):
            return f
    return None


def derive_storage_tokens(ports, stored):
    """从端口表推导存储接口关键词集合。"""
    toks = set()
    for p in ports:
        if p["port_type"] == "sata":
            toks.add("SATA")
        elif p["port_type"] == "apple_ssd_blade":
            spec = p["spec"]
            if "NVMe" in spec:
                toks.add("NVMe 刀片")
            elif "AHCI" in spec:
                toks.add("AHCI 刀片")
            elif "SATA" in spec:
                toks.add("SATA 刀片")
        elif p["port_type"] == "pcie_slot":
            toks.add("PCIe 槽")
    if "soldered" in (stored or ""):
        toks.add("焊接")
    return toks


def storage_matches(stored, toks):
    """存储字符串与端口推导的宽松一致性。"""
    s = stored or ""
    checks = {
        "SATA": "SATA" in s,
        "NVMe 刀片": "NVMe" in s,
        "AHCI 刀片": "AHCI" in s,
        "SATA 刀片": "SATA" in s,
        "PCIe 槽": "PCIe-slot" in s or "PCIe" in s,
        "焊接": "soldered" in s,
    }
    return all(checks.get(t, True) for t in toks)


def derive_nvme(m, ports):
    blades = [p["spec"] for p in ports if p["port_type"] == "apple_ssd_blade"]
    has_pcie_slot = any(p["port_type"] == "pcie_slot" for p in ports)
    si = m["storage_interface"] or ""
    if "soldered" in si:
        return "no"
    if any("NVMe" in b for b in blades):
        return "native"
    if any("AHCI" in b for b in blades):
        # 2013 起的 PCIe AHCI 刀片经 10.13+ BootROM 可引导 NVMe
        return "firmware_update_required" if m["release_year"] >= 2013 else "no"
    if has_pcie_slot:
        # 2010+ 塔式: BootROM 更新 (随 Mojave) 后 PCIe NVMe 可引导;
        # 2009 款 (4,1) 官方固件线停在 NVMe 之前, 需刷 5,1 (野路子) 或 OpenCore
        return "firmware_update_required" if m["release_year"] >= 2010 else "opencore_required"
    return "no"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    models = conn.execute("SELECT * FROM models ORDER BY model_identifier").fetchall()
    issues = 0

    print("1) family ← model_name 前缀:")
    for m in models:
        d = derive_family(m["model_name"])
        if d != m["family"]:
            issues += 1
            print(f"  ✗ {m['model_identifier']}: 存储 {m['family']!r} vs 推导 {d!r}")
    print("  (其余一致)\n")

    print("2) cpu_model ← cpu_options 基础款:")
    for m in models:
        base = conn.execute(
            """SELECT cpu_model, ghz, cores FROM cpu_options
               WHERE model_id=? AND config_type='standard' ORDER BY cores, ghz LIMIT 1""",
            (m["id"],)).fetchone()
        if base and base["cpu_model"] not in m["cpu_model"]:
            issues += 1
            print(f"  ✗ {m['model_identifier']}: 存储 {m['cpu_model']!r} vs 档位表基础款 {base['cpu_model']!r}")
    print("  (其余一致)\n")

    print("2b) stock_gpu ← gpu_options 基础款:")
    for m in models:
        base = conn.execute(
            """SELECT gpu_model FROM gpu_options
               WHERE model_id=? AND config_type='standard' ORDER BY id LIMIT 1""",
            (m["id"],)).fetchone()
        if base:
            # 宽松比对: 基础款关键词 (最后一个词组) 应出现在 stock_gpu 摘要里
            key = base["gpu_model"].split("+")[-1].strip().split(" (")[0]
            key = key.replace("Intel ", "").replace("NVIDIA ", "").replace("AMD ", "").replace("ATI ", "").replace("GeForce ", "").replace("Radeon ", "").replace("双 ", "")
            if key and key.split()[-1] not in (m["stock_gpu"] or ""):
                issues += 1
                print(f"  ✗ {m['model_identifier']}: 摘要 {m['stock_gpu']!r} vs 档位基础款 {base['gpu_model']!r}")
    print("  (其余一致)\n")

    print("3) storage_interface ← expansion_ports 推导:")
    for m in models:
        ports = conn.execute("SELECT * FROM expansion_ports WHERE model_id=?",
                             (m["id"],)).fetchall()
        toks = derive_storage_tokens(ports, m["storage_interface"])
        if ports and not storage_matches(m["storage_interface"], toks):
            issues += 1
            print(f"  ✗ {m['model_identifier']}: 存储 {m['storage_interface']!r} vs 端口推导 {sorted(toks)}")
        elif not ports:
            print(f"  ⚠ {m['model_identifier']}: 无端口数据, 无法对账 (存储 {m['storage_interface']!r})")
    print("  (其余一致)\n")

    print("4) nvme_bootable ← 刀片类型+年代规则:")
    for m in models:
        ports = conn.execute("SELECT * FROM expansion_ports WHERE model_id=?",
                             (m["id"],)).fetchall()
        if not ports:
            continue
        d = derive_nvme(m, ports)
        if d != m["nvme_bootable"]:
            issues += 1
            print(f"  ✗ {m['model_identifier']}: 存储 {m['nvme_bootable']!r} vs 规则推导 {d!r}")
    print("  (其余一致)\n")

    print("5) cpu_socket 平台内大类一致性 (BGA/LGA; 具体封装如 1023/1224 归机型级):")
    rows = conn.execute("""
        SELECT p.name,
          GROUP_CONCAT(DISTINCT CASE WHEN m.cpu_socket LIKE 'BGA%' THEN 'BGA'
                                     WHEN m.cpu_socket LIKE 'LGA%' THEN 'LGA'
                                     ELSE m.cpu_socket END) AS kinds,
          COUNT(DISTINCT CASE WHEN m.cpu_socket LIKE 'BGA%' THEN 'BGA'
                              WHEN m.cpu_socket LIKE 'LGA%' THEN 'LGA'
                              ELSE m.cpu_socket END) AS n
        FROM models m JOIN platforms p ON p.id = m.platform_id
        GROUP BY p.id HAVING n > 1""").fetchall()
    for r in rows:
        issues += 1
        print(f"  ✗ 平台 {r['name']}: 大类混装 {r['kinds']}")
    if not rows:
        print("  全部平台内大类一致 (同平台 BGA 细分不同属正常, 如 Sandy 移动含 1023/1224)")

    conn.close()
    print(f"\n结论: {issues} 处不一致" if issues else "\n结论: 全部对账通过")
    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
