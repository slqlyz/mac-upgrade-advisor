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

USAGES = ["轻度日用", "黑苹果续命", "秀肌肉", "野路子"]
WEIGHT_THRESHOLD = 0.3  # 低于此权重的类别对该用途无意义, 直接不推荐

# 每档用途的规则:
#   classes: 收录哪类方案 (standard=正常参考 / unorthodox=非常规, 如 iMac 换 E3)
#   egpu: eGPU 是外接扩展而非机器本身升级, 全部归 秀肌肉 (TB3 官方 + TB1/2 脚本);
#         野路子作为超集自然继承
#   derived: 允许哪些理论推导
# 野路子是秀肌肉的严格超集 (= 秀肌肉全部内容 + 非常规实证),
# 且是彩蛋性质: 没有非常规实证的机器不解锁该档 (见 wild_extras)。
USAGE_RULES = {
    "轻度日用":    {"classes": {"standard"}, "egpu": set(),   "derived": {"ram_max"}},
    "黑苹果续命":  {"classes": {"standard"}, "egpu": set(),   "derived": {"gpu_internal", "ram_max"}},
    "秀肌肉":      {"classes": {"standard"}, "egpu": {"tb3", "tb12"},
                    "derived": {"ram_max", "gpu_internal", "gpu_egpu"}},
    "野路子":      {"classes": {"standard", "unorthodox"}, "egpu": {"tb3", "tb12"},
                    "derived": {"ram_max", "gpu_internal", "gpu_egpu"}},
}


def is_laptop(model):
    return model["family"] in ("MacBook", "MacBook Air", "MacBook Pro")


def flex_available(model):
    """秀肌肉档是否适用: 笔记本无 CPU/显卡/eGPU 拉满空间, 不设此档。"""
    return not is_laptop(model)


# BGA 板级改装判定机制的实证锚点 (类可行性依据, 逐机型实证另行入库)
BGA_SWAP_ANCHORS = [
    "https://forums.macrumors.com/threads/creating-the-worlds-first-quad-core-mid-2012-13-inch-macbook-pro.2388731/",
    "https://www.reddit.com/r/mac/comments/1hquwht/the_we_have_it_folks_i73615qe_4c8t_and_16gb_ram/",
    "https://lowendmac.com/2024/ultimate-hybrid-2011-2012-17-macbook-pro/",
]


def bga_quad_swap_applicable(model, plat):
    """同代 BGA CPU 换装判定: SNB/IVB 平台 + BGA 封装 CPU。
    依据: M 系与 QE 系共用 BGA1023 封装, 且 Apple 未从固件删除 QE 微码;
    跨代 (SNB 板上 IVB) 亦有实证但需 CoreBoot 换固件。"""
    if not (model["cpu_socket"] or "").startswith("BGA") or not plat:
        return False
    arch = plat.get("cpu_microarch") or ""
    return "Sandy Bridge" in arch or "Ivy Bridge" in arch


def ram_reball_floor(model):
    """焊接内存机型的颗粒加焊可达值 (严密下界) = max(16GB, 官方上限)。
    2012+ 世代加焊 16GB 有实证锚点 (2012 Air 8→16)。
    注意: 控制器纸面上限对焊接机型不可套用 — 加焊只能把现有颗粒位换更大颗粒,
    不能新增主板从未布线的颗粒位/通道 (如 3615QM 控制器写 32GB, 板载只布一通道
    的位就到不了 32)。不适用时返回 None。"""
    if not ram_reball_applicable(model):
        return None
    return max(16, model["official_max_ram_gb"])


def ram_reball_applicable(model):
    """内存颗粒加焊判定: 焊接内存 + 2012–2019 (末代 Intel)。
    2012 前的 Air (A1369/A1370) 部分主板缺高容量数据线, 排除。"""
    return model["ram_slots"] == 0 and 2012 <= model["release_year"] <= 2019


def wild_extras(ctx):
    """野路子相对秀肌肉的独有内容是否存在 (解锁条件):
    非常规实证, 或本机可用的非常规显卡架构 (如 Titan V 撤回版驱动, 需 PCIe 槽
    且机器能运行其驱动窗口)。eGPU 已全部归秀肌肉; BGA 板级改装暂不解锁 (无实证)。"""
    if any((r.get("path_class") or "standard") == "unorthodox"
           for r in ctx["compat"]):
        return True
    m = ctx["model"]
    if bga_quad_swap_applicable(m, ctx.get("platform")) or ram_reball_applicable(m):
        return True
    if any(p["port_type"] == "pcie_slot" for p in ctx["ports"]):
        ship_key = m["release_year"] - 2004
        win_off = parse_versions(m["max_macos"])
        off_key = (win_off[1] or win_off[0]) if win_off else None
        for g in ctx.get("gpu_archs", []):
            if (g.get("path_class") or "standard") != "unorthodox":
                continue
            native = parse_versions(g["macos_native"])
            if not native:
                continue
            if ((native[1] is None or native[1] >= ship_key)
                    and (off_key is None or native[0] <= off_key)):
                return True
    return False


def physical_ram_max(model, plat):
    """机器级内存物理可达上限 = min(控制器上限, 槽数 × 该世代单条最大容量)。
    ARK 的数字是控制器上限 (假设所有槽插满当代最大模组); 机器实际能到多少
    还受槽数和该内存技术现实存在的单条容量限制 (如 DDR3 单条最大 8GB,
    2 槽机型控制器写 32GB 也只能物理到 16GB)。焊接内存或缺数据时返回 None。"""
    if not plat or model["ram_slots"] == 0:
        return None
    ctrl = plat["controller_max_ram_gb"]
    mod = plat.get("max_module_gb")
    if not ctrl or not mod:
        return ctrl
    return min(ctrl, model["ram_slots"] * mod)


RISKS = ["official", "community", "experimental"]  # 递进包含

LAYER_ORDER = {"official": 0, "community_tested": 1, "experimental": 2, "derived": 3}
LAYER_FACTOR = {"official": 1.0, "community_tested": 0.9, "experimental": 0.7, "derived": 0.5}

# 用途 → 组件类别权重 (编辑规则)
WEIGHTS = {
    "轻度日用":   {"ssd": 1.0, "ram": 0.7, "adapter": 0.4, "cpu": 0.2, "gpu": 0.1,
                   "hdd": 0.2, "wifi_bt_card": 0.3, "optical_bay_caddy": 0.3},
    "黑苹果续命": {"gpu": 1.0, "wifi_bt_card": 0.9, "ssd": 0.8, "adapter": 0.5,
                   "ram": 0.5, "cpu": 0.2, "hdd": 0.1, "optical_bay_caddy": 0.2},
    "秀肌肉":     {"ram": 1.0, "cpu": 1.0, "gpu": 1.0, "ssd": 1.0, "adapter": 0.8,
                   "wifi_bt_card": 0.6, "hdd": 0.4, "optical_bay_caddy": 0.6},
    "野路子":     {"ram": 1.0, "cpu": 1.0, "gpu": 1.0, "ssd": 1.0, "adapter": 1.0,
                   "wifi_bt_card": 1.0, "hdd": 1.0, "optical_bay_caddy": 1.0,
                   "other": 1.0},  # 固件刷写等非硬件类野路子
}

# 用途 × 类别 → "为什么值得升" (编辑文案)
WHY = {
    ("轻度日用", "ssd"): "机械盘/旧盘换 SSD 是日常响应速度提升最大的一项",
    ("轻度日用", "ram"): "16GB+ 即可满足多标签浏览与办公套件, 无需拉满 (老机型按官方上限尽量给足)",
    ("黑苹果续命", "gpu"): "OCLP 跑新系统的生死线: 显卡必须支持 Metal 且驱动可补 (见架构表 Metal 列)",
    ("黑苹果续命", "wifi_bt_card"): "新系统的隔空投送/接力依赖较新的无线卡",
    ("黑苹果续命", "ssd"): "NVMe 化后新系统体验才完整",
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


LATEST_X86_MACOS = "26"  # Tahoe, Intel Mac 支持的最后一版 macOS


def oclp_applicable(model):
    """黑苹果续命是否适用: 官方最高系统已是 x86 末代 (Tahoe 26) 的机型无命可续。"""
    win = parse_versions(model["max_macos"])
    if not win:
        return True
    return (win[1] or win[0]) < _ver_key(LATEST_X86_MACOS)


def oclp_targets(model, versions):
    """黑苹果续命可选的目标系统: OCLP 支持且高于该机官方最高系统。"""
    win = parse_versions(model["max_macos"])
    cur = (win[1] or win[0]) if win else 0
    return [v for v in versions
            if v["oclp_supported"] and _ver_key(v["version"]) > cur]


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
    ctx["macos_versions"] = _rows(conn, "SELECT * FROM macos_versions ORDER BY id")
    ctx["all_platforms"] = _rows(conn, "SELECT * FROM platforms")
    ctx["models_min"] = _rows(conn,
        "SELECT model_identifier, platform_id, nvme_bootable FROM models")
    return ctx


def diagnose(ctx, usage):
    """瓶颈诊断: 对该用途, 这台机器缺什么/强什么。"""
    m, plat = ctx["model"], ctx["platform"]
    out = []
    # 内存
    evid_max = max([r["max_working_capacity_gb"] or 0 for r in ctx["compat"]
                    if r["category"] == "ram"] + [0])
    phys = physical_ram_max(m, plat)
    if m["ram_slots"] == 0:
        out.append(f"内存焊接, 出厂多少就是多少 (本机 {m['official_max_ram_gb']}GB 档为上限), 无常规升级空间")
    elif evid_max > m["official_max_ram_gb"]:
        out.append(f"内存有隐藏空间: 官方标 {m['official_max_ram_gb']}GB, 社区实测 {evid_max}GB 可用"
                   + (f" (物理可达 {phys}GB = {m['ram_slots']}槽 × 单条最大 {plat['max_module_gb']}GB)"
                      if phys else ""))
    elif phys and phys > m["official_max_ram_gb"]:
        out.append(f"内存物理可达 {phys}GB ({m['ram_slots']}槽 × 单条最大 {plat['max_module_gb']}GB, "
                   f"控制器上限 {plat['controller_max_ram_gb']}GB) 高于官方 {m['official_max_ram_gb']}GB, "
                   "但本机型暂无超规格实证")
    # 存储
    stock = m["stock_storage"] or ""
    si = m["storage_interface"]
    hdd_stock = "HDD" in stock or "Fusion" in stock
    sata_only = "SATA" in si and "blade" not in si and "PCIe" not in si
    if hdd_stock and sata_only:
        out.append(f"原装存储含机械盘 ({stock}), 换 SATA SSD 是第一顺位升级 (总线上限 ~550MB/s)")
    elif hdd_stock:
        out.append(f"原装存储含机械盘 ({stock}), 换 SSD 是几乎所有用途的第一顺位升级")
    if si.startswith("soldered"):
        out.append("存储焊接 (T2), 内部不可升级, 扩容走外置")
    elif sata_only and not hdd_stock:
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
    if ctx["gpu_paths"]:
        is_laptop = m["family"] in ("MacBook", "MacBook Air", "MacBook Pro")
        paths_show = [p_ for p_ in ctx["gpu_paths"] if not (is_laptop and "eGPU" in p_)]
        if paths_show:
            out.append(f"显卡升级路径: {' / '.join(paths_show)}")
        if is_laptop and usage in ("秀肌肉", "野路子") and any("eGPU" in p_ for p_ in ctx["gpu_paths"]):
            out.append("顺带一提: 本机有雷电口, 技术上可 eGPU, 但笔记本外接显卡违背便携定位, 不列为推荐 (eGPU 秀肌肉仅适用台式机)")
    if usage == "黑苹果续命":
        if m["max_macos"]:
            out.append(f"官方最高系统 {m['max_macos']}, 更新系统需 OCLP, 硬件建议以驱动区间为准")
        if m["oclp_caveat"]:
            line = f"OCLP 兼容性 (本机): 原装显卡 {m['stock_gpu']} — {m['oclp_caveat']}"
            internal_path = any(p_ not in ("",) and "eGPU" not in p_ for p_ in ctx["gpu_paths"])
            if not m["stock_gpu_metal"]:
                line += " → 有 MXM/PCIe 位可换 Metal 卡根治" if internal_path                     else " → 显卡不可换, 上新系统请慎重评估体验"
            out.append(line)
    return out


def _attach_warnings(ctx, category, interface=None, name_hint=None):
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
        # 型号专属约束 (如 Xeon 睡眠缺陷) 不挂到不相关组件上
        if "Xeon" in c["description"] and c["constraint_type"] == "sleep_quirk" \
                and name_hint and "Xeon" not in name_hint:
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
    # 内存超官方推导: 物理可达 (min(控制器, 槽数×单条)) 高于官方, 且无超规格实证。
    # 目标按用途封顶: 轻度日用/黑苹果只在官方上限够不到 16GB 基线时出手,
    # 推荐 min(物理可达, 16GB); 秀肌肉/野路子直接拉满到物理可达
    phys = physical_ram_max(m, plat)
    if ("ram_max" in rules["derived"] and phys
            and phys > m["official_max_ram_gb"]
            and not any(r["category"] == "ram" and (r["max_working_capacity_gb"] or 0) > m["official_max_ram_gb"]
                        for r in ctx["compat"])):
        if usage in ("轻度日用", "黑苹果续命"):
            tgt_gb = min(phys, 16) if m["official_max_ram_gb"] < 16 else None
            title = (f"内存实际可上 {tgt_gb}GB (官方标 {m['official_max_ram_gb']}GB 偏保守)"
                     if tgt_gb else None)
        else:
            tgt_gb = phys
            title = f"内存理论可至 {phys}GB ({m['ram_slots']}槽 × 单条最大 {plat['max_module_gb']}GB)"
        if title:
            items.append({
                "category": "ram", "title": title,
                "min_risk": "community",  # 依据 ARK 控制器规格+单条容量, 推导可靠性高
                "notes": f"物理可达 {phys}GB = min(控制器上限 {plat['controller_max_ram_gb']}GB, "
                         f"{m['ram_slots']}槽 × 单条最大 {plat['max_module_gb']}GB); "
                         f"本机型无实证 (同代机型此类超官方配置为社区常规操作); 需 {m['ram_type']} 规格",
                "sources": [plat["controller_source_url"]] if plat["controller_source_url"] else [],
            })
    # 显卡: 按用途选档 — 黑苹果续命取满足所选目标系统的最低档 (够亮就行, 无需拉满);
    # 秀肌肉/野路子的可达系统含 OCLP 续命后的版本 (真正拉满); MXM 机型限制在有 MXM 卡的架构
    target = ctx.get("target")
    target_meta = ctx.get("target_meta")
    official_key = None
    win = parse_versions(m["max_macos"])
    if win:
        official_key = win[1] or win[0]
    ship_key = m["release_year"] - 2004  # 出厂系统近似 (机器能运行的最低 macOS)
    # 秀肌肉/野路子的可达上限: OCLP 可续命的机器按 OCLP 支持的最高版计
    oclp_max = max((_ver_key(v["version"]) for v in ctx.get("macos_versions", [])
                    if v["oclp_supported"]), default=None)
    flex_reach = oclp_max if (usage in ("秀肌肉", "野路子") and oclp_max
                              and oclp_applicable(m)) else official_key

    def pick_arch(mxm_only, minimize, wild=False):
        cands = []
        for g in ctx["gpu_archs"]:
            if not (g.get("perf_rank") or 0):
                continue  # rank 0: 无驱动 (Turing+), 不参与推荐
            if ((g.get("path_class") or "standard") == "unorthodox") != wild:
                continue  # 非常规架构 (如撤回驱动的 Volta) 只进野路子独有位
            if mxm_only and not g.get("mxm_available"):
                continue
            native = parse_versions(g["macos_native"])
            patched = parse_versions(g.get("macos_patched") or "")
            # 机器可运行区间约束: 架构窗口的下限须可达, 上限须不低于出厂系统
            if native and native[1] is not None and native[1] < ship_key:
                continue  # 该机出厂系统已高于此架构的驱动终点 (如 2019 Mac Pro 跑不了 10.13)
            if usage == "黑苹果续命" and target:
                if target_meta and target_meta["metal_required"]:
                    ms = g.get("metal_support") or ""
                    if "支持" not in ms:
                        continue  # 该目标系统强制 Metal
                tkey = target[0]
                def covers(w):
                    return w and w[0] <= tkey and (w[1] is None or tkey <= w[1])
                if not (covers(native) or covers(patched)):
                    continue  # 原生或补丁窗口须覆盖所选目标系统
            elif native and flex_reach:
                if native[0] > flex_reach:
                    continue  # 连 OCLP 续命后也够不到该架构的最低系统要求
            cands.append(g)
        if not cands:
            return None
        return (min if minimize else max)(cands, key=lambda g: g["perf_rank"])

    def oclp_note(g):
        """架构最低系统高于官方最高时, 提示需先 OCLP 续命。"""
        native = parse_versions(g["macos_native"])
        if native and official_key and native[0] > official_key:
            return f"; 需先经 OCLP 升至 macOS {native[2]}+ (官方最高 {m['max_macos']})"
        return ""

    def gpu_item(paths, g, title, extra_note=""):
        return {
            "category": "gpu", "title": title,
            "notes": f"架构: {g['vendor']} {g['arch']} | 原生驱动 {g['macos_native']}"
                     + (f" | 补丁后 {g['macos_patched']}" if g.get("macos_patched") else "")
                     + (f" | Metal: {g['metal_support']}" if g.get("metal_support") else "")
                     + f"; 路径: {' / '.join(paths)}; 具体卡型无本机型实证" + extra_note,
            "sources": [g["source_url"]] + json.loads(g.get("extra_source_urls") or "[]"),
        }

    # 机内显卡路径推导: 已有机内显卡实证时不再重复推导
    internal = [p for p in ctx["gpu_paths"] if "eGPU" not in p]
    if "gpu_internal" in rules["derived"] and internal and "gpu" not in have_cat:
        mxm_only = all("MXM" in p for p in internal)
        if usage == "黑苹果续命":
            g = pick_arch(mxm_only, minimize=True)
            if g:
                it = gpu_item(internal, g,
                    f"显卡换装: 上 Metal 门槛卡即可 — {g['arch']} (如 {g['entry_cards']})",
                    "; 续命只求 Metal 达标, 无需拉满性能")
                it["min_risk"] = "community"  # 依据驱动区间+Metal 要求, 推导可靠性高
                items.append(it)
        else:
            g = pick_arch(mxm_only, minimize=False)
            if g:
                it = gpu_item(internal, g,
                    f"显卡拉满: {g['arch']} (如 {g['flagship_cards']}) — 可达系统内驱动最强架构",
                    oclp_note(g))
                it["min_risk"] = "community"  # 依据社区验证的驱动区间数据, 推导可靠性高
                items.append(it)
        # 野路子独有: 非常规架构 (如 Titan V 的撤回版驱动), 叠加在常规拉满之上
        if usage == "野路子" and not all("MXM" in p for p in internal):
            g = pick_arch(mxm_only=False, minimize=False, wild=True)
            if g:
                it = gpu_item(internal, g,
                    f"真野路子显卡: {g['arch']} ({g['flagship_cards']})",
                    oclp_note(g))
                it["wild_exclusive"] = True
                items.append(it)
    # eGPU 是独立路径, 不被机内显卡实证阻挡; 归秀肌肉档且仅限台式机
    # (笔记本外接显卡违背便携定位, 只在诊断里提一嘴, 见 diagnose)
    is_laptop = m["family"] in ("MacBook", "MacBook Air", "MacBook Pro")
    egpu = [p for p in ctx["gpu_paths"] if "eGPU" in p]
    egpu_kind = "tb3" if any("TB3" in p for p in egpu) else ("tb12" if egpu else None)
    if ("gpu_egpu" in rules["derived"] and egpu and egpu_kind in rules["egpu"]
            and not is_laptop):
        g = pick_arch(mxm_only=False, minimize=False)
        if g:
            it = gpu_item(egpu, g,
                f"eGPU 拉满: {g['arch']} (如 {g['flagship_cards']})",
                "; 注意: eGPU 是外接扩展, 不是机器本身的升级"
                + ("; TB1/2 需社区脚本且带宽受限" if egpu_kind == "tb12" else "")
                + oclp_note(g))
            if egpu_kind == "tb3":
                it["min_risk"] = "community"  # TB3 eGPU 是 Apple 官方支持机制
            items.append(it)
    # BGA 判定机制推导 (仅野路子): 类可行性有实证锚点, 本机无实证时以推导呈现
    if usage == "野路子":
        have_unorthodox = {r["category"] for r in ctx["compat"]
                           if (r.get("path_class") or "standard") == "unorthodox"}
        if bga_quad_swap_applicable(m, plat) and "cpu" not in have_unorthodox:
            arch = plat.get("cpu_microarch") or ""
            chips = ("i7-2715QE 等" if "Sandy" in arch
                     else "i7-3615QE ($26 级白菜) / 3612QE (35W 散热更优) / 更高档 QM")
            items.append({
                "category": "cpu", "wild_exclusive": True,
                "title": f"BGA CPU 板级换装可行 — 同代同封装 (可选 {chips})",
                "notes": "判定依据: SNB/IVB 的 M 系与 QE 系共用 BGA1023 封装, 且 Apple 未删 QE 微码 "
                         "(实证锚点: 2012 Air 四核化 / MBP 13吋 2012 四核化); 跨代 (SNB 板上 IVB CPU) "
                         "亦有实证但需 CoreBoot 换固件 (17吋 2011 杂交); 专业改装级, 需 BGA 返修台; "
                         "散热按原 TDP 设计, 45W 芯片持续负载会降频",
                "sources": list(BGA_SWAP_ANCHORS),
            })
        if ram_reball_applicable(m) and "ram" not in have_unorthodox:
            floor = ram_reball_floor(m)
            items.append({
                "category": "ram", "wild_exclusive": True,
                "title": f"内存颗粒加焊扩容 (板级) — 可达 {floor}GB",
                "notes": f"判定依据: 2012–2019 焊接内存机型的颗粒均可拆焊换更大容量 "
                         f"(实证锚点: 2012 Air 8→16GB, Reddit; 深圳维修圈常规操作); "
                         "上限受主板颗粒位/通道布线约束 — 加焊只能换更大颗粒, 不能新增未布线的位, "
                         "控制器纸面值不可直接套用; 2012 前的 Air (A1369/A1370) 缺高容量数据线, 不适用; 专业改装级",
                "sources": [BGA_SWAP_ANCHORS[1]],
            })
    # 固件解锁链 (仅野路子): 非常规条目声明 unlocks_platform 时,
    # 用解锁后的平台重算内存物理可达 (如 4,1 刷 5,1 后 Westmere 解锁 16GB RDIMM)
    if usage == "野路子":
        by_name = {p_["name"]: p_ for p_ in ctx.get("all_platforms", [])}
        for r in ctx["compat"]:
            alt_name = r.get("unlocks_platform")
            if not alt_name or (r.get("path_class") or "standard") != "unorthodox":
                continue
            alt = by_name.get(alt_name)
            if not alt:
                continue
            alt_phys = physical_ram_max(m, alt)
            cur_phys = physical_ram_max(m, plat)
            if alt_phys and (not cur_phys or alt_phys > cur_phys):
                # 解锁后的 NVMe 引导状态: 取解锁平台上原生机型的值 (如 4,1 刷后按 5,1 计)
                sib = next((x for x in ctx.get("models_min", [])
                            if x["platform_id"] == alt["id"]
                            and x["model_identifier"] != m["model_identifier"]), None)
                nvme_note = ""
                if sib and sib["nvme_bootable"] != m["nvme_bootable"]:
                    lbl = {"native": "原生支持", "firmware_update_required": "需固件更新",
                           "opencore_required": "需 OpenCore", "no": "不支持"}
                    nvme_note = (f"; NVMe 引导同步解锁: {lbl.get(m['nvme_bootable'], m['nvme_bootable'])}"
                                 f" → {lbl.get(sib['nvme_bootable'], sib['nvme_bootable'])}"
                                 f" (按 {sib['model_identifier']} 计)")
                items.append({
                    "category": "ram", "wild_exclusive": True,
                    "title": f"刷固件解锁后: 内存物理可达 {alt_phys}GB "
                             f"({m['ram_slots']}槽 × 单条 {alt['max_module_gb']}GB)",
                    "notes": f"前提: 完成「{r.get('part_model') or '固件解锁'}」; 解锁后按平台 "
                             f"{alt_name} 计 (原生固件上限 {cur_phys}GB)" + nvme_note
                             + "; 完整的刷后方案 (CPU 换装/实证) 参见对应机型页",
                    "sources": [r["source_url"]] + ([alt["controller_source_url"]] if alt.get("controller_source_url") else []),
                })
    return items


def advise(identifier, usage, risk, target=None, db_path=DB_PATH):
    """target: 黑苹果续命的目标系统版本 (如 '12'/'15'), 缺省取 OCLP 支持的最高版;
    其他用途忽略此参数 (目标恒为该机官方最高系统)。"""
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
    # 目标系统: 黑苹果续命由用户选择 (缺省 = OCLP 支持的最高版); 其他用途 = 官方最高
    target_err = None
    if usage == "黑苹果续命":
        cands = oclp_targets(ctx["model"], ctx["macos_versions"])
        by_ver = {v["version"]: v for v in cands}
        if target and target not in by_ver:
            target_err = (f"目标系统 {target!r} 不可选 (需为 OCLP 支持且高于本机官方最高的版本: "
                          f"{[v['version'] for v in cands]})")
        if not cands:
            return {
                "model_identifier": ctx["model"]["model_identifier"],
                "model_name": ctx["model"]["model_name"],
                "usage": usage, "risk": risk, "target_macos": None,
                "diagnosis": [], "recommendations": [],
                "empty_hint": f"本机官方最高 {ctx['model']['max_macos']}, OCLP 暂无更高的受支持版本可续 (Tahoe 支持推进中)",
                "mutual_warnings": [], "hidden_by_risk": 0, "irrelevant_skipped": 0,
                "wild_available": wild_ok, "target_options": [],
                "disclaimer": "",
            }
        meta = by_ver.get(target) if target else (cands[-1] if cands else None)
        ctx["target"] = (_ver_key(meta["version"]), meta["version"]) if meta else None
        ctx["target_meta"] = meta
        ctx["target_options"] = [{"version": v["version"], "name": v["name"]} for v in cands]
    else:
        ctx["target"] = target_macos(ctx["model"], usage)
        ctx["target_meta"] = None
        ctx["target_options"] = []
    if target_err:
        return {"error": target_err}
    if usage == "秀肌肉" and not flex_available(ctx["model"]):
        return {
            "model_identifier": ctx["model"]["model_identifier"],
            "model_name": ctx["model"]["model_name"],
            "usage": usage, "risk": risk, "target_macos": None,
            "diagnosis": [], "recommendations": [],
            "empty_hint": "笔记本不设秀肌肉档 (无 CPU/显卡/eGPU 拉满空间); 硬盘内存类升级见「轻度日用」",
            "mutual_warnings": [], "hidden_by_risk": 0, "irrelevant_skipped": 0,
            "wild_available": wild_ok, "target_options": [],
            "disclaimer": "",
        }
    if usage == "黑苹果续命" and not oclp_applicable(ctx["model"]):
        return {
            "model_identifier": ctx["model"]["model_identifier"],
            "model_name": ctx["model"]["model_name"],
            "usage": usage, "risk": risk, "target_macos": None,
            "diagnosis": [], "recommendations": [],
            "empty_hint": f"本机官方最高系统已是 x86 末代 (macOS {LATEST_X86_MACOS}), 不存在续命需求",
            "mutual_warnings": [], "hidden_by_risk": 0, "irrelevant_skipped": 0,
            "wild_available": wild_ok,
            "disclaimer": "",
        }
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
        # 原厂选配同款组件: 官方性由 Apple 规格页背书 (配置官方存在且固件原生支持),
        # 展示层恒为 official — 同一操作不能因风险档不同而变色;
        # 社区实证降格为"换装实录"补充信息 (数据库分层不变)
        factory_part = False
        if r["category"] == "cpu" and r.get("part_model"):
            factory_part = any(r["part_model"] in (o["cpu_model"] or "")
                               for o in ctx["cpu_options"])
        eff_layer = "official" if factory_part else r["confidence_level"]
        if eff_layer not in allowed:
            hidden += 1
            continue
        w = weights.get(r["category"], 0.2)
        if w < WEIGHT_THRESHOLD:
            irrelevant += 1  # 该类别对此用途无意义 (如轻度日用换显卡), 不推荐
            continue
        notes = r["notes"]
        if factory_part:
            notes = ("出厂选配同款配置, 官方固件原生支持; 换装实录与注意事项来自社区: "
                     + (notes or ""))
        recs.append({
            "layer": eff_layer,
            "category": r["category"],
            "title": _component_name(r),
            "factory_part": factory_part,
            "wild_exclusive": (r.get("path_class") or "standard") == "unorthodox",
            "why": _why(usage, r["category"]),
            "result": r["result"],
            "max_working_capacity_gb": r["max_working_capacity_gb"],
            "verified_macos_versions": r["verified_macos_versions"],
            "requires_adapter": bool(r["requires_adapter"]),
            "notes": notes,
            "sources": ([ctx["model"]["apple_spec_url"]] if factory_part else [])
                       + [r["source_url"]] + json.loads(r["extra_source_urls"] or "[]"),
            "warnings": _attach_warnings(ctx, r["category"], r["interface"],
                                         name_hint=_component_name(r)),
            "score": w * LAYER_FACTOR[eff_layer],
        })
    if risk != "official":
        for d in _derived_items(ctx, usage):
            # 推导项默认只在 experimental 显示; 高可靠推导 (如黑苹果显卡换装,
            # 依据驱动区间+Metal 要求) 可声明 min_risk=community 提前显示
            if d.get("min_risk", "experimental") == "experimental" and risk != "experimental":
                continue
            recs.append({
                "layer": "derived", "category": d["category"], "title": d["title"],
                "wild_exclusive": d.get("wild_exclusive", False),
                "why": _why(usage, d["category"]), "result": None,
                "max_working_capacity_gb": None, "verified_macos_versions": None,
                "requires_adapter": False, "notes": d["notes"], "sources": d["sources"],
                "warnings": _attach_warnings(ctx, d["category"]),
                "score": weights.get(d["category"], 0.2) * LAYER_FACTOR["derived"],
            })
    # 官方配置层: 原厂内存/CPU 最高规格是 Apple 规格页上的官方事实,
    # 任何风险级可见; 同类实证已在列时让位, 避免重复
    m = ctx["model"]
    if (m["ram_slots"] > 0 and weights.get("ram", 0) >= WEIGHT_THRESHOLD
            and not any(r["category"] == "ram" for r in recs)):
        if usage == "轻度日用" and m["official_max_ram_gb"] > 16:
            ram_title = (f"内存按官方规格加装即可 (轻度日用 16GB+ 够; "
                         f"官方上限 {m['official_max_ram_gb']}GB, {m['ram_type']})")
        else:
            ram_title = f"内存官方规格可配至 {m['official_max_ram_gb']}GB ({m['ram_type']})"
        recs.append({
            "layer": "official", "category": "ram",
            "title": ram_title,
            "wild_exclusive": False, "why": _why(usage, "ram"), "result": "works",
            "max_working_capacity_gb": m["official_max_ram_gb"],
            "verified_macos_versions": None, "requires_adapter": False,
            "notes": f"插槽 ×{m['ram_slots']}; Apple 官方认可的配置上限, "
                     "超官方规格的空间见社区实证/理论推导条目",
            "sources": [m["apple_spec_url"]],
            "warnings": _attach_warnings(ctx, "ram"),
            "score": weights.get("ram", 0) * LAYER_FACTOR["official"],
        })
    if ((m["cpu_socket"] or "").startswith("LGA")
            and weights.get("cpu", 0) >= WEIGHT_THRESHOLD
            and not any(r["category"] == "cpu" for r in recs)
            and len(ctx["cpu_options"]) > 1):
        t = max(ctx["cpu_options"], key=lambda o: (o["cores"], o["ghz"]))
        recs.append({
            "layer": "official", "category": "cpu",
            "title": f"CPU 官方配置上限: {t['cpu_model']} ({t['ghz']}GHz {t['cores']}核, 出厂选配同款)",
            "wild_exclusive": False, "why": _why(usage, "cpu"), "result": "works",
            "max_working_capacity_gb": None, "verified_macos_versions": None,
            "requires_adapter": False,
            "notes": "该配置为官方出厂选配, 固件原生支持 (微码必然在表内); "
                     "注意: 自行换装操作本身不是 Apple 支持的行为, 实证与风险见社区层",
            "sources": [m["apple_spec_url"]],
            "warnings": _attach_warnings(ctx, "cpu", name_hint=t["cpu_model"]),
            "score": weights.get("cpu", 0) * LAYER_FACTOR["official"],
        })
    # 原装含机械盘 + 有 SATA 位 + 尚无 SSD 推荐 → 标准接口推导 (SATA 为行业标准,
    # 推导可靠性高, 故在 community 风险级即显示; official 级仍隐藏)
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

    # 系统版本一致性校验 (版本语义比较, 非浮点), 目标 = 用户所选/官方最高
    target = ctx.get("target")
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
        "target_options": ctx.get("target_options", []),
        "disclaimer": "排序权重为编辑规则; 事实性数据 (实证/约束/冲突/驱动区间) 均带来源可溯; "
                      "理论推导项无实证, 风险自担",
    }
