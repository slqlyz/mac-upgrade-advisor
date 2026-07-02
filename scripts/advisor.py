#!/usr/bin/env python3
"""推荐引擎核心 (第三阶段, v1)。零依赖, 供 advise.py (CLI) 和 serve.py (Web) 共用。

原则:
  - 事实性数据 (兼容实证/约束/冲突/驱动区间) 全部来自数据库, 带来源;
    用途画像的权重与文案是编辑规则, 输出中明确注明。
  - 风险过滤是硬分界: official < community_tested < experimental < derived(理论推导)。
  - 理论推导项只在运行时计算, 永远不写入数据库, 且明确标注"无实证"。
"""

import json
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "mac_upgrade.db"

USAGES = ["轻度日用", "黑苹果续命", "Windows双系统", "秀肌肉", "野路子"]
WEIGHT_THRESHOLD = 0.3  # 低于此权重的类别对该用途无意义, 直接不推荐

# 每档用途的规则:
#   classes: 收录哪类方案 (standard=正常参考 / unorthodox=非常规, 如 iMac 换 E3)
#   egpu: eGPU 是外接扩展而非机器本身升级, 全部归 秀肌肉 (TB3 官方 + TB1/2 脚本);
#         野路子作为超集自然继承
#   derived: 允许哪些理论推导
# 野路子是秀肌肉的严格超集 (= 秀肌肉全部内容 + 非常规实证),
# 且是彩蛋性质: 没有非常规实证的机器不解锁该档 (见 wild_extras)。
USAGE_RULES = {
    "轻度日用":    {"classes": {"standard"}, "egpu": set(),   "derived": set()},
    "黑苹果续命":  {"classes": {"standard"}, "egpu": set(),   "derived": {"gpu_internal"}},
    "Windows双系统": {"classes": {"standard"}, "egpu": set(), "derived": {"gpu_internal"}},
    "秀肌肉":      {"classes": {"standard"}, "egpu": {"tb3", "tb12"},
                    "derived": {"ram_max", "cpu_top", "gpu_internal", "gpu_egpu"}},
    "野路子":      {"classes": {"standard", "unorthodox"}, "egpu": {"tb3", "tb12"},
                    "derived": {"ram_max", "cpu_top", "gpu_internal", "gpu_egpu"}},
}


def wild_extras(ctx):
    """野路子相对秀肌肉的独有内容是否存在 (解锁条件): 仅非常规实证。
    eGPU 已全部归秀肌肉; BGA 板级改装暂不解锁 (无 ≥1 来源实证, 只作约束说明;
    有实证后以 experimental+unorthodox 收录并自动解锁)。"""
    return any((r.get("path_class") or "standard") == "unorthodox"
               for r in ctx["compat"])
RISKS = ["official", "community", "experimental"]  # 递进包含

LAYER_ORDER = {"official": 0, "community_tested": 1, "experimental": 2, "derived": 3}
LAYER_FACTOR = {"official": 1.0, "community_tested": 0.9, "experimental": 0.7, "derived": 0.5}

# 用途 → 组件类别权重 (编辑规则)
WEIGHTS = {
    "轻度日用":   {"ssd": 1.0, "ram": 0.7, "adapter": 0.4, "cpu": 0.2, "gpu": 0.1,
                   "hdd": 0.2, "wifi_bt_card": 0.3, "optical_bay_caddy": 0.3},
    "黑苹果续命": {"gpu": 1.0, "wifi_bt_card": 0.9, "ssd": 0.8, "adapter": 0.5,
                   "ram": 0.5, "cpu": 0.2, "hdd": 0.1, "optical_bay_caddy": 0.2},
    "Windows双系统": {"ssd": 0.9, "ram": 0.7, "optical_bay_caddy": 0.7, "gpu": 0.5,
                    "adapter": 0.5, "cpu": 0.4, "wifi_bt_card": 0.4, "hdd": 0.3},
    "秀肌肉":     {"ram": 1.0, "cpu": 1.0, "gpu": 1.0, "ssd": 1.0, "adapter": 0.8,
                   "wifi_bt_card": 0.6, "hdd": 0.4, "optical_bay_caddy": 0.6},
    "野路子":     {"ram": 1.0, "cpu": 1.0, "gpu": 1.0, "ssd": 1.0, "adapter": 1.0,
                   "wifi_bt_card": 1.0, "hdd": 1.0, "optical_bay_caddy": 1.0},
}

# 用途 × 类别 → "为什么值得升" (编辑文案)
WHY = {
    ("轻度日用", "ssd"): "机械盘/旧盘换 SSD 是日常响应速度提升最大的一项",
    ("轻度日用", "ram"): "8–16GB 即可满足多标签浏览与办公套件, 不必拉满",
    ("黑苹果续命", "gpu"): "OCLP 跑新系统的生死线: 显卡必须支持 Metal 且驱动可补 (见架构表 Metal 列)",
    ("黑苹果续命", "wifi_bt_card"): "新系统的隔空投送/接力依赖较新的无线卡",
    ("黑苹果续命", "ssd"): "NVMe 化后新系统体验才完整",
    ("Windows双系统", "ssd"): "第二块盘装 Windows 最稳, Boot Camp 与 macOS 共盘分区易出问题",
    ("Windows双系统", "optical_bay_caddy"): "光驱位托架是加第二块盘的经典路径",
    ("Windows双系统", "ram"): "双系统并存, 内存宽裕些好",
    ("Windows双系统", "gpu"): "Windows 下显卡驱动不受本库 macOS 驱动区间限制; macOS 侧仍按区间表",
    ("秀肌肉", "ram"): "拉满: 直奔实测/控制器上限, 不考虑性价比",
    ("秀肌肉", "cpu"): "拉满: 该插槽能上的最高档",
    ("秀肌肉", "gpu"): "拉满: 该路径下驱动区间内的最强架构",
    ("秀肌肉", "ssd"): "拉满: 容量与速度都到顶",
}


def _why(usage, category):
    """野路子是秀肌肉的超集, 常规条目文案沿用秀肌肉的。"""
    if usage == "野路子":
        return WHY.get(("野路子", category)) or WHY.get(("秀肌肉", category), "")
    return WHY.get((usage, category), "")

# 推荐类别 → 需要附着的约束类型
CATEGORY_CONSTRAINTS = {
    "cpu": {"cpu_firmware_check", "sleep_quirk"},
    "gpu": {"egpu_support", "gpu_driver"},
    "ssd": {"nvme_boot"},
}


def _ver_key(tok):
    """macOS 版本语义排序键: 10.6→6, 10.13→13, 10.15→15, 11→16, 12→17 … 26→31。
    浮点直接比较是错的 (10.6 > 10.13), 必须按版本语义映射。"""
    if tok.startswith("10."):
        return int(tok[3:].split(".")[0])
    return int(float(tok)) + 5


def parse_versions(text):
    """从 '10.13–12.6' / 'macOS Monterey 12' 等文本提取版本区间。
    返回 (min_key, max_key|None, min_str, max_str|None); 解析不了返回 None。"""
    if not text:
        return None
    toks = re.findall(r"(?<![.\d])((?:1[0-9]|2[0-6])(?:\.\d{1,2})?)(?![\d])", text)
    toks = [t for t in toks if t.startswith("10.") or 11 <= float(t) <= 26]
    if not toks:
        return None
    toks.sort(key=_ver_key)
    open_ended = any(k in text for k in ("起", "+", "及以上"))
    lo, hi = toks[0], toks[-1]
    return (_ver_key(lo), None if open_ended else _ver_key(hi),
            lo, None if open_ended else hi)


def target_macos(model, usage):
    """该用途的目标系统: 黑苹果续命 = 尽量新 (OCLP); 其余 = 官方最高系统。
    返回 (排序键, 显示文本) 或 None。"""
    if usage == "黑苹果续命":
        return (_ver_key("26"), "26")
    win = parse_versions(model["max_macos"])
    if not win:
        return None
    return (win[1] or win[0], win[3] or win[2])


def _rows(conn, sql, params=()):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def load_context(conn, identifier):
    ms = _rows(conn, "SELECT * FROM models WHERE model_identifier = ? COLLATE NOCASE",
               (identifier,))
    if not ms:
        return None
    m = ms[0]
    ctx = {"model": m}
    ctx["platform"] = (_rows(conn, "SELECT * FROM platforms WHERE id=?", (m["platform_id"],))
                       or [None])[0] if m["platform_id"] else None
    ctx["ports"] = _rows(conn, "SELECT * FROM expansion_ports WHERE model_id=?", (m["id"],))
    ctx["cpu_options"] = _rows(conn, "SELECT * FROM cpu_options WHERE model_id=?", (m["id"],))
    ctx["compat"] = _rows(conn, """
        SELECT c.*, co.category, co.manufacturer, co.part_model, co.is_generic,
               co.interface, co.capacity_gb, co.speed_spec, co.requires_adapter
        FROM compatibility c JOIN components co ON co.id = c.component_id
        WHERE c.model_id = ?""", (m["id"],))
    ctx["conflicts"] = _rows(conn, """
        SELECT k.*, a.category AS a_category, a.interface AS a_interface,
               a.manufacturer AS a_manufacturer, a.part_model AS a_part_model
        FROM known_conflicts k JOIN components a ON a.id = k.component_a_id
        WHERE k.model_id = ?""", (m["id"],))

    tb = [p["spec"] for p in ctx["ports"] if p["port_type"] == "thunderbolt"]
    flags = {
        "has_tb3": any("Thunderbolt 3" in x for x in tb),
        "has_tb1_or_tb2": any(("Thunderbolt 1" in x or "Thunderbolt 2" in x) for x in tb),
        "bga_cpu_or_soldered_ram": (m["cpu_socket"] or "").startswith("BGA") or m["ram_slots"] == 0,
    }
    ctx["flags"] = flags
    cons = _rows(conn, """
        SELECT * FROM hw_constraints
        WHERE (scope='model' AND model_id=?) OR (scope='platform' AND platform_id=?)
           OR scope='global'""", (m["id"], m["platform_id"] or -1))
    ctx["constraints"] = [c for c in cons
                          if not c["applicability"] or flags.get(c["applicability"], True)]

    paths = []
    if any(p["port_type"] == "mxm" for p in ctx["ports"]):
        paths.append("MXM 显卡位")
    if any(p["port_type"] == "pcie_slot" for p in ctx["ports"]):
        paths.append("PCIe 插槽")
    if flags["has_tb3"]:
        paths.append("eGPU (TB3, 官方支持)")
    elif flags["has_tb1_or_tb2"]:
        paths.append("eGPU (TB1/2, 非官方需社区脚本)")
    ctx["gpu_paths"] = paths
    ctx["gpu_archs"] = _rows(conn, "SELECT * FROM gpu_arch_support ORDER BY vendor, arch") \
        if paths else []
    return ctx


def diagnose(ctx, usage):
    """瓶颈诊断: 对该用途, 这台机器缺什么/强什么。"""
    m, plat = ctx["model"], ctx["platform"]
    out = []
    # 内存
    evid_max = max([r["max_working_capacity_gb"] or 0 for r in ctx["compat"]
                    if r["category"] == "ram"] + [0])
    real_max = evid_max or m["official_max_ram_gb"]
    if m["ram_slots"] == 0:
        out.append(f"内存焊接, 出厂多少就是多少 (本机 {m['official_max_ram_gb']}GB 档为上限), 无常规升级空间")
    elif evid_max > m["official_max_ram_gb"]:
        out.append(f"内存有隐藏空间: 官方标 {m['official_max_ram_gb']}GB, 社区实测 {evid_max}GB 可用"
                   + (f" (控制器上限 {plat['controller_max_ram_gb']}GB)" if plat and plat["controller_max_ram_gb"] else ""))
    elif plat and plat["controller_max_ram_gb"] and plat["controller_max_ram_gb"] > m["official_max_ram_gb"]:
        out.append(f"控制器理论上限 {plat['controller_max_ram_gb']}GB 高于官方 {m['official_max_ram_gb']}GB, 但本机型暂无超规格实证")
    # 存储
    stock = m["stock_storage"] or ""
    if "HDD" in stock or "Fusion" in stock:
        out.append(f"原装存储含机械盘 ({stock}), 换 SSD 是几乎所有用途的第一顺位升级")
    si = m["storage_interface"]
    if si.startswith("soldered"):
        out.append("存储焊接 (T2), 内部不可升级, 扩容走外置")
    elif "SATA" in si and "blade" not in si and "PCIe" not in si:
        out.append("存储为 SATA 总线, 换 SSD 后速度封顶约 550MB/s")
    elif "AHCI" in si:
        out.append(f"专有刀片槽 (AHCI), NVMe 化: {m['nvme_bootable']}")
    # CPU
    sock = m["cpu_socket"] or ""
    if sock.startswith("LGA"):
        n_opts = len([o for o in ctx["cpu_options"] if o["config_type"] == "configurable"])
        out.append(f"CPU 为 {sock} 插槽, 物理可换 (同代固件约束适用, 有 {n_opts} 档出厂选配可参照)")
    elif sock.startswith("BGA"):
        out.append("CPU 为 BGA 焊接, 常规不可升级")
    # GPU
    if usage in ("视频剪辑", "黑苹果续命") and ctx["gpu_paths"]:
        out.append(f"显卡升级路径: {' / '.join(ctx['gpu_paths'])}")
    if usage == "黑苹果续命" and m["max_macos"]:
        out.append(f"官方最高系统 {m['max_macos']}, 更新系统需 OCLP, 硬件建议以驱动区间为准")
    return out


def _attach_warnings(ctx, category, interface=None):
    warns = []
    for k in ctx["conflicts"]:
        if k["a_category"] == category or (interface and interface in (k["a_interface"] or "")):
            warns.append({
                "kind": "conflict",
                "text": f"已知冲突 ({k['severity']}): {k['description']}"
                        + (f" 规避: {k['workaround']}" if k["workaround"] else ""),
                "sources": [k["source_url"]] + json.loads(k["extra_source_urls"] or "[]"),
            })
    for c in ctx["constraints"]:
        if c["constraint_type"] not in CATEGORY_CONSTRAINTS.get(category, set()):
            continue
        # eGPU 约束只附着到走雷电路径的显卡方案, 内置 MXM/PCIe 卡不相关
        if c["constraint_type"] == "egpu_support" and interface and \
                not any(k in interface for k in ("eGPU", "Thunderbolt", "TB")):
            continue
        warns.append({
            "kind": "constraint",
            "text": f"约束 ({c['constraint_type']}): {c['description']}",
            "sources": [c["source_url"]] + json.loads(c["extra_source_urls"] or "[]"),
        })
    return warns


def _component_name(r):
    if r["is_generic"]:
        base = f"(泛型) {r['interface']}"
    else:
        base = " ".join(x for x in (r["manufacturer"], r["part_model"]) if x)
    extra = [x for x in (f"{r['capacity_gb']}GB" if r["capacity_gb"] else None,
                         r["speed_spec"]) if x]
    return base + (f" [{', '.join(extra)}]" if extra else "")


def _derived_items(ctx, usage):
    """理论推导项 (只在 risk=experimental 时出现), 全部标注无实证。
    按用途规则门控: 拉满类推导只归 秀肌肉; eGPU 归 秀肌肉(TB3)/野路子(TB1/2)。"""
    m, plat = ctx["model"], ctx["platform"]
    rules = USAGE_RULES[usage]
    items = []
    # 推导门控只看常规实证: 保证野路子 = 秀肌肉超集 (非常规实证不挡任何推导)
    have_cat = {r["category"] for r in ctx["compat"]
                if (r.get("path_class") or "standard") == "standard"}
    # 内存拉满: 控制器高于官方, 有插槽, 且无实证
    if ("ram_max" in rules["derived"] and m["ram_slots"] > 0 and plat
            and plat["controller_max_ram_gb"]
            and plat["controller_max_ram_gb"] > m["official_max_ram_gb"]
            and not any(r["category"] == "ram" and (r["max_working_capacity_gb"] or 0) > m["official_max_ram_gb"]
                        for r in ctx["compat"])):
        items.append({
            "category": "ram",
            "title": f"内存理论可至 {plat['controller_max_ram_gb']}GB (控制器上限)",
            "notes": f"依据 {plat['name']} 平台内存控制器规格推导, 本机型无实证; 需 {m['ram_type']} 规格",
            "sources": [plat["controller_source_url"]] if plat["controller_source_url"] else [],
        })
    # CPU 顶配: LGA 插槽且无换装实证 → 参照出厂选配档
    if ("cpu_top" in rules["derived"] and (m["cpu_socket"] or "").startswith("LGA")
            and "cpu" not in have_cat):
        tops = [o for o in ctx["cpu_options"] if o["config_type"] == "configurable"]
        if tops:
            t = max(tops, key=lambda o: (o["cores"], o["ghz"]))
            items.append({
                "category": "cpu",
                "title": f"CPU 理论可换至出厂顶配同款 ({t['cpu_model']}, {t['ghz']}GHz {t['cores']}核)",
                "notes": "同插槽且在固件微码表内 (出厂即有此配置), 但用户自行换装无本机型实证; 拆装难度视机型而定",
                "sources": [m["apple_spec_url"]],
            })
    # 显卡: 机内路径 (MXM/PCIe) 与外接 eGPU 分开门控
    def gpu_item(paths, archs, extra_note=""):
        if not paths or not archs:
            return None
        names = ", ".join(g["arch"] for g in archs[:3])
        return {
            "category": "gpu",
            "title": f"显卡路径 ({' / '.join(paths)}) 理论可用架构: {names}",
            "notes": "依据 GPU 架构×macOS 驱动区间推导, 具体卡型无本机型实证; "
                     "目标系统版本决定可选架构" + extra_note,
            "sources": list({g["source_url"] for g in archs}),
        }

    amd_ok = [g for g in ctx["gpu_archs"]
              if g["vendor"] == "AMD" and "从未" not in g["macos_native"]]
    # 机内显卡路径推导: 已有机内显卡实证时不再重复推导
    internal = [p for p in ctx["gpu_paths"] if "eGPU" not in p]
    if "gpu_internal" in rules["derived"] and internal and "gpu" not in have_cat:
        archs = amd_ok
        note = ""
        if usage == "黑苹果续命":
            # Metal 是 OCLP 上新系统的硬门槛, 且排除驱动止步 10.13 的架构
            archs = [g for g in ctx["gpu_archs"] if g.get("metal_support")
                     and "支持" in g["metal_support"] and "止步" not in g["metal_support"]]
            note = "; 黑苹果续命场景已按 Metal 支持过滤 (非 Metal 卡在新系统是死路)"
        it = gpu_item(internal, archs, note)
        if it:
            items.append(it)
    # eGPU 是独立路径, 不被机内显卡实证阻挡
    egpu = [p for p in ctx["gpu_paths"] if "eGPU" in p]
    egpu_kind = "tb3" if any("TB3" in p for p in egpu) else ("tb12" if egpu else None)
    if "gpu_egpu" in rules["derived"] and egpu and egpu_kind in rules["egpu"]:
        it = gpu_item(egpu, amd_ok, "; 注意: eGPU 是外接扩展, 不是机器本身的升级"
                      + ("; TB1/2 需社区脚本且带宽受限" if egpu_kind == "tb12" else ""))
        if it:
            items.append(it)
    return items


def advise(identifier, usage, risk, db_path=DB_PATH):
    assert usage in USAGES, f"用途须为: {USAGES}"
    if usage == "野路子":
        risk = "experimental"  # 野路子默认拉满风险, 不受风险偏好参数影响
    assert risk in RISKS, f"风险须为: {RISKS}"
    conn = sqlite3.connect(db_path)
    ctx = load_context(conn, identifier)
    conn.close()
    if ctx is None:
        return None

    allowed = {"official"}
    if risk in ("community", "experimental"):
        allowed.add("community_tested")
    if risk == "experimental":
        allowed.add("experimental")

    weights = WEIGHTS[usage]
    rules = USAGE_RULES[usage]
    wild_ok = wild_extras(ctx)
    if usage == "野路子" and not wild_ok:
        # 彩蛋性质: 没有独有内容的机器不解锁
        return {
            "model_identifier": ctx["model"]["model_identifier"],
            "model_name": ctx["model"]["model_name"],
            "usage": usage, "risk": risk, "target_macos": None,
            "diagnosis": [], "recommendations": [],
            "empty_hint": "本机没有野路子 (彩蛋性质, 仅特殊机器解锁); 常规拉满方案请看「秀肌肉」",
            "mutual_warnings": [], "hidden_by_risk": 0, "irrelevant_skipped": 0,
            "wild_available": False,
            "disclaimer": "",
        }
    recs, hidden, irrelevant = [], 0, 0
    for r in ctx["compat"]:
        if (r.get("path_class") or "standard") not in rules["classes"]:
            irrelevant += 1  # 正常用途不掺非常规方案
            continue
        if r["confidence_level"] not in allowed:
            hidden += 1
            continue
        w = weights.get(r["category"], 0.2)
        if w < WEIGHT_THRESHOLD:
            irrelevant += 1  # 该类别对此用途无意义 (如轻度日用换显卡), 不推荐
            continue
        recs.append({
            "layer": r["confidence_level"],
            "category": r["category"],
            "title": _component_name(r),
            "wild_exclusive": (r.get("path_class") or "standard") == "unorthodox",
            "why": _why(usage, r["category"]),
            "result": r["result"],
            "max_working_capacity_gb": r["max_working_capacity_gb"],
            "verified_macos_versions": r["verified_macos_versions"],
            "requires_adapter": bool(r["requires_adapter"]),
            "notes": r["notes"],
            "sources": [r["source_url"]] + json.loads(r["extra_source_urls"] or "[]"),
            "warnings": _attach_warnings(ctx, r["category"], r["interface"]),
            "score": w * LAYER_FACTOR[r["confidence_level"]],
        })
    if risk == "experimental":
        for d in _derived_items(ctx, usage):
            recs.append({
                "layer": "derived", "category": d["category"], "title": d["title"],
                "wild_exclusive": d.get("wild_exclusive", False),
                "why": _why(usage, d["category"]), "result": None,
                "max_working_capacity_gb": None, "verified_macos_versions": None,
                "requires_adapter": False, "notes": d["notes"], "sources": d["sources"],
                "warnings": _attach_warnings(ctx, d["category"]),
                "score": weights.get(d["category"], 0.2) * LAYER_FACTOR["derived"],
            })
    # 原装含机械盘 + 有 SATA 位 + 尚无 SSD 推荐 → 标准接口推导 (SATA 为行业标准,
    # 推导可靠性高, 故在 community 风险级即显示; official 级仍隐藏)
    m = ctx["model"]
    stock = m["stock_storage"] or ""
    if (risk != "official"
            and ("HDD" in stock or "Fusion" in stock)
            and any(p["port_type"] == "sata" for p in ctx["ports"])
            and not any(r["category"] == "ssd" for r in recs)
            and weights.get("ssd", 0) >= WEIGHT_THRESHOLD):
        recs.append({
            "layer": "derived", "category": "ssd",
            "title": "原装机械盘换装 SATA SSD (标准接口推导)",
            "wild_exclusive": False,
            "why": _why(usage, "ssd"), "result": None,
            "max_working_capacity_gb": None, "verified_macos_versions": None,
            "requires_adapter": False,
            "notes": f"原装存储为 {stock}; SATA 是行业标准接口, 任意 2.5\"/3.5\" SATA SSD "
                     "物理与协议均兼容, 推导可靠性高 (速度封顶约 550MB/s); 具体拆装难度视机型而定",
            "sources": [m["apple_spec_url"]],
            "warnings": _attach_warnings(ctx, "ssd", "SATA"),
            "score": weights.get("ssd", 0) * 0.85,
        })

    # 系统版本一致性校验 (版本语义比较, 非浮点)
    target = target_macos(m, usage)
    windows = []
    for r in recs:
        win = parse_versions(r["verified_macos_versions"])
        if win:
            windows.append((r["title"], win))
            if target and win[1] is not None and win[1] < target[0]:
                r["warnings"].append({
                    "kind": "os",
                    "text": f"系统版本注意: 该方案验证区间上限为 macOS {win[3]}, "
                            f"低于目标系统 (macOS {target[1]}); 更高版本上未经验证, 可能无法使用",
                    "sources": [],
                })
    mutual = []
    for i in range(len(windows)):
        for j in range(i + 1, len(windows)):
            (t1, w1), (t2, w2) = windows[i], windows[j]
            if (w1[1] is not None and w1[1] < w2[0]) or (w2[1] is not None and w2[1] < w1[0]):
                mutual.append(f"方案互斥: 「{t1}」(验证区间 {w1[2]}–{w1[3] or '最新'}) 与 "
                              f"「{t2}」(验证区间 {w2[2]}–{w2[3] or '最新'}) 的可用系统不重叠, "
                              "无法在同一系统上同时受益")
    recs.sort(key=lambda x: (-x["score"], LAYER_ORDER[x["layer"]]))

    return {
        "model_identifier": m["model_identifier"],
        "model_name": m["model_name"],
        "usage": usage,
        "risk": risk,
        "target_macos": target,
        "diagnosis": diagnose(ctx, usage),
        "recommendations": recs,
        "empty_hint": ("本机暂无已收录的野路子方案 (野路子只对部分特殊机器存在)"
                       if usage == "野路子" and not recs else
                       "当前用途与风险偏好下没有可推荐项"),
        "mutual_warnings": mutual,
        "hidden_by_risk": hidden,
        "irrelevant_skipped": irrelevant,
        "wild_available": wild_ok,
        "disclaimer": "排序权重为编辑规则; 事实性数据 (实证/约束/冲突/驱动区间) 均带来源可溯; "
                      "理论推导项无实证, 风险自担",
    }
