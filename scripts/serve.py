#!/usr/bin/env python3
"""图形化界面: 本地 Web UI (零依赖, 标准库 http.server + sqlite3)。

用法:
    python3 scripts/serve.py               # 启动并自动打开浏览器
    python3 scripts/serve.py --port 9000   # 指定端口
    python3 scripts/serve.py --no-browser  # 不自动开浏览器

仅监听 127.0.0.1, 不对外网提供服务。
"""

import argparse
import json
import sqlite3
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import advisor

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "mac_upgrade.db"


def query_db(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


def api_models():
    return query_db(
        """SELECT m.model_identifier, m.model_name, m.release_year, m.family,
                  COUNT(DISTINCT c.id) AS n_compat, COUNT(DISTINCT k.id) AS n_conflicts
           FROM models m
           LEFT JOIN compatibility c ON c.model_id = m.id
           LEFT JOIN known_conflicts k ON k.model_id = m.id
           GROUP BY m.id ORDER BY m.family, m.release_year"""
    )


def api_model_detail(identifier):
    models = query_db(
        "SELECT * FROM models WHERE model_identifier = ? COLLATE NOCASE", (identifier,)
    )
    if not models:
        return None
    m = models[0]
    compat = query_db(
        """SELECT c.*, co.category, co.manufacturer, co.part_model, co.is_generic,
                  co.interface, co.capacity_gb, co.speed_spec, co.requires_adapter,
                  co.notes AS component_notes
           FROM compatibility c JOIN components co ON co.id = c.component_id
           WHERE c.model_id = ?""",
        (m["id"],),
    )
    for r in compat:
        r["extra_source_urls"] = json.loads(r.get("extra_source_urls") or "[]")
    conflicts = query_db(
        """SELECT k.*, a.manufacturer, a.part_model, a.is_generic, a.interface
           FROM known_conflicts k JOIN components a ON a.id = k.component_a_id
           WHERE k.model_id = ?""",
        (m["id"],),
    )
    for r in conflicts:
        r["extra_source_urls"] = json.loads(r.get("extra_source_urls") or "[]")
    layers = {"official": [], "community_tested": [], "experimental": []}
    for r in compat:
        layers[r["confidence_level"]].append(r)
    cpu_options = query_db(
        """SELECT * FROM cpu_options WHERE model_id = ?
           ORDER BY config_type = 'configurable', cores, ghz""",
        (m["id"],),
    )
    platform = None
    if m.get("platform_id"):
        rows = query_db("SELECT * FROM platforms WHERE id = ?", (m["platform_id"],))
        platform = rows[0] if rows else None
    ports = query_db(
        "SELECT * FROM expansion_ports WHERE model_id = ? ORDER BY port_type", (m["id"],)
    )
    constraints = query_db(
        """SELECT * FROM hw_constraints
           WHERE (scope='model' AND model_id=?) OR (scope='platform' AND platform_id=?)
              OR scope='global'
           ORDER BY scope, constraint_type""",
        (m["id"], m.get("platform_id") or -1),
    )
    # 按机型硬件条件过滤约束适用性 (雷电版本 / BGA 焊接)
    tb_specs = [p["spec"] for p in ports if p["port_type"] == "thunderbolt"]
    ctx = {
        "has_tb3": any("Thunderbolt 3" in x for x in tb_specs),
        "has_tb1_or_tb2": any(("Thunderbolt 1" in x or "Thunderbolt 2" in x) for x in tb_specs),
        "bga_cpu_or_soldered_ram": (m.get("cpu_socket") or "").startswith("BGA") or m.get("ram_slots") == 0,
    }
    constraints = [r for r in constraints
                   if not r.get("applicability") or ctx.get(r["applicability"], True)]
    for r in constraints:
        r["extra_source_urls"] = json.loads(r.get("extra_source_urls") or "[]")
    # 本机显卡升级路径逐条判定, 有任何一条才返回 GPU 架构速查表
    gpu_paths = []
    if any(p["port_type"] == "mxm" for p in ports):
        gpu_paths.append("MXM 显卡位")
    if any(p["port_type"] == "pcie_slot" for p in ports):
        gpu_paths.append("PCIe 插槽")
    if ctx["has_tb3"]:
        gpu_paths.append("eGPU (TB3, 官方支持)")
    elif ctx["has_tb1_or_tb2"]:
        gpu_paths.append("eGPU (TB1/2, 非官方需社区脚本)")
    gpu_archs = []
    if gpu_paths:
        gpu_archs = query_db(
            "SELECT * FROM gpu_arch_support ORDER BY vendor, arch")
        for r in gpu_archs:
            r["extra_source_urls"] = json.loads(r.get("extra_source_urls") or "[]")
    wild_available = advisor.wild_extras(
        {"compat": compat, "model": m, "ports": ports, "gpu_archs": gpu_archs})
    flex_ok = advisor.flex_available(m)
    versions = query_db("SELECT * FROM macos_versions ORDER BY id")
    oclp_target_opts = [{"version": v["version"], "name": v["name"]}
                        for v in advisor.oclp_targets(m, versions)]
    return {"model": m, "compatibility": layers, "conflicts": conflicts,
            "cpu_options": cpu_options, "platform": platform, "ports": ports,
            "constraints": constraints, "gpu_archs": gpu_archs, "gpu_paths": gpu_paths,
            "wild_available": wild_available,
            "oclp_applicable": advisor.oclp_applicable(m) and len(oclp_target_opts) > 0,
            "flex_available": flex_ok, "oclp_targets": oclp_target_opts}


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>x86 Mac 硬件升级顾问</title>
<style>
  :root {
    --bg: #f5f5f7; --card: #fff; --text: #1d1d1f; --muted: #6e6e73;
    --line: #e5e5ea; --accent: #0071e3;
    --official: #1a7f37; --official-bg: #e6f4ea;
    --community: #b25000; --community-bg: #fff3e6;
    --experimental: #b02a37; --experimental-bg: #fdecee;
  }
  * { box-sizing: border-box; margin: 0; }
  body { font: 14px/1.6 -apple-system, "PingFang SC", "Helvetica Neue", sans-serif;
         background: var(--bg); color: var(--text); }
  header { padding: 14px 24px; background: var(--card); border-bottom: 1px solid var(--line);
           display: flex; align-items: baseline; gap: 12px; }
  header h1 { font-size: 17px; }
  header span { color: var(--muted); font-size: 12px; }
  .layout { display: flex; height: calc(100vh - 53px); }
  aside { width: 320px; min-width: 320px; background: var(--card);
          border-right: 1px solid var(--line); display: flex; flex-direction: column; }
  aside input { margin: 12px; padding: 8px 12px; border: 1px solid var(--line);
                border-radius: 8px; font-size: 13px; outline: none; }
  aside input:focus { border-color: var(--accent); }
  #model-list { overflow-y: auto; flex: 1; }
  .family { padding: 8px 16px 2px; font-size: 11px; font-weight: 600;
            color: var(--muted); text-transform: uppercase; letter-spacing: .5px; }
  .model-item { padding: 8px 16px; cursor: pointer; border-left: 3px solid transparent; }
  .model-item:hover { background: var(--bg); }
  .model-item.active { background: #eaf3fd; border-left-color: var(--accent); }
  .model-item .name { font-size: 13px; }
  .model-item .meta { font-size: 11px; color: var(--muted); }
  .count-badge { display: inline-block; min-width: 18px; text-align: center;
                 background: var(--line); border-radius: 9px; font-size: 11px;
                 padding: 0 5px; margin-left: 6px; }
  .count-badge.has { background: var(--accent); color: #fff; }
  main { flex: 1; overflow-y: auto; padding: 24px; }
  .placeholder { color: var(--muted); text-align: center; margin-top: 15vh; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
          padding: 18px 20px; margin-bottom: 16px; }
  .card h2 { font-size: 18px; margin-bottom: 2px; }
  .card .sub { color: var(--muted); font-size: 12px; margin-bottom: 14px; }
  .spec-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
               gap: 10px 18px; }
  .spec-grid .k { font-size: 11px; color: var(--muted); }
  .spec-grid .v { font-size: 13px; }
  h3.layer { font-size: 14px; margin: 18px 0 8px; display: flex; align-items: center; gap: 8px; }
  .badge { font-size: 11px; font-weight: 600; padding: 2px 10px; border-radius: 20px; }
  .badge.official { color: var(--official); background: var(--official-bg); }
  .badge.community_tested { color: var(--community); background: var(--community-bg); }
  .badge.experimental { color: var(--experimental); background: var(--experimental-bg); }
  .badge.derived { color: #5856d6; background: #eeeefc; }
  .entry.l-derived { border-left: 4px solid #5856d6; }
  .adv-controls { display: flex; gap: 10px; margin: 10px 0 4px; flex-wrap: wrap; align-items: center; }
  .adv-controls select { padding: 6px 10px; border: 1px solid var(--line); border-radius: 8px;
                         font-size: 13px; background: var(--card); }
  .adv-controls button { padding: 6px 16px; border: none; border-radius: 8px; font-size: 13px;
                         background: var(--accent); color: #fff; cursor: pointer; }
  .adv-controls button:hover { opacity: .85; }
  .entry { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px;
           margin-bottom: 10px; background: var(--card); }
  .entry.l-official { border-left: 4px solid var(--official); }
  .entry.l-community_tested { border-left: 4px solid var(--community); }
  .entry.l-experimental { border-left: 4px solid var(--experimental); }
  .entry .title { font-weight: 600; font-size: 13px; }
  .entry .tags { margin: 4px 0; }
  .tag { display: inline-block; font-size: 11px; background: var(--bg);
         border-radius: 6px; padding: 1px 8px; margin-right: 6px; color: var(--muted); }
  .entry .notes { font-size: 12px; color: var(--text); margin: 6px 0;
                  background: var(--bg); border-radius: 6px; padding: 6px 10px; }
  .sources { font-size: 11px; }
  .sources a { color: var(--accent); text-decoration: none; word-break: break-all;
               display: block; }
  .sources a:hover { text-decoration: underline; }
  .conflict { border: 1px solid #f1c0c5; background: #fffafa; }
  .empty-layer { color: var(--muted); font-size: 12px; margin-bottom: 10px; }
  .warn-title { color: var(--experimental); }
</style>
</head>
<body>
<header>
  <h1>x86 Mac 硬件升级顾问</h1>
  <span>数据分层: 官方支持 / 社区验证 (≥2 独立来源) / 实验性 · 所有条目可溯源</span>
</header>
<div class="layout">
  <aside>
    <input id="search" type="search" placeholder="搜索机型 (名称或标识)…">
    <div style="padding:0 16px 6px;font-size:11px;color:var(--muted)">蓝色数字 = 已收录的实证条目数 (升级方案 + 风险案例)</div>
    <div id="model-list"></div>
  </aside>
  <main id="detail"><div class="placeholder">← 从左侧选择一款机型</div></main>
</div>
<script>
const LAYER_LABEL = { official: "官方支持", community_tested: "社区验证 (≥2 独立来源)",
                      experimental: "实验性 (孤例, 风险自担)", derived: "理论推导 (无实证)" };
const USAGE_OPTS = ["轻度日用", "黑苹果续命", "秀肌肉", "野路子"];
const RISK_OPTS = [["official", "仅官方支持"], ["community", "接受社区验证"],
                   ["experimental", "接受实验性 + 理论推导"]];
const RESULT_LABEL = { works: "可用", works_with_caveats: "可用但有注意事项",
                       partial: "部分可用", failed: "失败案例" };
const NVME_LABEL = { native: "原生支持", firmware_update_required: "需固件更新",
                     opencore_required: "需 OpenCore", no: "不支持" };
const PORT_LABEL = { pcie_slot: "PCIe 插槽", thunderbolt: "雷电", sata: "SATA 位",
                     sodimm_slot: "内存插槽", mxm: "MXM 显卡位", apple_ssd_blade: "专有 SSD 刀片槽",
                     usb: "USB", optical_bay: "光驱位", firewire: "FireWire", sd_card: "SD 卡槽" };
const CONSTRAINT_LABEL = { cpu_firmware_check: "CPU 固件校验", nvme_boot: "NVMe 引导",
                           egpu_support: "eGPU 支持", gpu_driver: "GPU 驱动",
                           sleep_quirk: "睡眠缺陷", bandwidth_share: "带宽共享", other: "其他" };
const SEVERITY_LABEL = { no_boot: "无法启动", instability: "不稳定",
                         performance_degradation: "性能下降", feature_loss: "功能损失", cosmetic: "轻微" };
const CAT_LABEL = { ram: "内存", ssd: "固态硬盘", hdd: "机械硬盘", wifi_bt_card: "无线网卡",
                    gpu: "显卡", cpu: "处理器", optical_bay_caddy: "光驱位托架",
                    adapter: "转接卡", battery: "电池", display: "屏幕", other: "其他" };
let allModels = [], activeId = null;

const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function compName(r) {
  const base = r.is_generic ? `(泛型) ${r.interface}`
    : [r.manufacturer, r.part_model].filter(Boolean).join(" ");
  const extra = [r.capacity_gb ? r.capacity_gb + "GB" : null, r.speed_spec]
    .filter(Boolean).join(", ");
  return base + (extra ? ` [${extra}]` : "");
}

function sourcesHtml(r) {
  const urls = [r.source_url, ...(r.extra_source_urls || [])];
  return `<div class="sources">${urls.map(u =>
    `<a href="${esc(u)}" target="_blank" rel="noopener">↗ ${esc(u)}</a>`).join("")}</div>`;
}

function renderList() {
  const q = document.getElementById("search").value.trim().toLowerCase();
  const el = document.getElementById("model-list");
  let html = "", family = null;
  for (const m of allModels) {
    const hay = (m.model_name + " " + m.model_identifier).toLowerCase();
    if (q && !q.split(/\\s+/).every(w => hay.includes(w))) continue;
    if (m.family !== family) { family = m.family; html += `<div class="family">${esc(family)}</div>`; }
    const n = m.n_compat + m.n_conflicts;
    html += `<div class="model-item${m.model_identifier === activeId ? " active" : ""}"
      onclick="showModel('${esc(m.model_identifier)}')">
      <div class="name">${esc(m.model_name)}${n ? `
        <span class="count-badge has" title="已收录实证条目数 (升级方案+冲突)">${n}</span>` : ""}</div>
      <div class="meta">${esc(m.model_identifier)} · ${m.release_year}</div></div>`;
  }
  el.innerHTML = html || `<div class="family">无匹配机型</div>`;
}

function entryHtml(r, layer) {
  return `<div class="entry l-${layer}">
    <div class="title">[${CAT_LABEL[r.category] || esc(r.category)}] ${esc(compName(r))} — ${RESULT_LABEL[r.result] || esc(r.result)}</div>
    <div class="tags">
      ${r.max_working_capacity_gb ? `<span class="tag">实测上限 ${r.max_working_capacity_gb}GB</span>` : ""}
      ${r.verified_macos_versions ? `<span class="tag">验证系统: ${esc(r.verified_macos_versions)}</span>` : ""}
      ${r.requires_adapter ? `<span class="tag">需转接卡</span>` : ""}
      ${r.corroboration_count > 1 ? `<span class="tag">${r.corroboration_count} 个独立来源</span>` : ""}
    </div>
    ${r.notes ? `<div class="notes">${esc(r.notes)}</div>` : ""}
    ${sourcesHtml(r)}</div>`;
}

async function showModel(id) {
  activeId = id; renderList();
  const d = await (await fetch("/api/model?id=" + encodeURIComponent(id))).json();
  const m = d.model;
  let html = `<div class="card">
    <h2>${esc(m.model_name)}</h2>
    <div class="sub">${esc(m.model_identifier)} · ${m.release_year} 年</div>
    <div class="spec-grid">
      <div><div class="k">CPU (基础款)</div><div class="v">${esc(m.cpu_model)}</div></div>
      <div><div class="k">CPU 插槽 / 升级性</div><div class="v">${esc(m.cpu_socket || "未知")}${
        m.cpu_socket ? (m.cpu_socket.indexOf("BGA") === 0
          ? ' · <span style="color:var(--experimental)">常规不可升级</span>'
          : ' · <span style="color:var(--official)">插槽式, 可物理换装</span>') : ""}</div></div>
      <div><div class="k">内存 (官方上限 / 物理可达)</div><div class="v">${m.official_max_ram_gb}GB / ${(() => {
        const p = d.platform;
        if (!p || !p.controller_max_ram_gb || !m.ram_slots) return "—";
        const phys = p.max_module_gb ? Math.min(p.controller_max_ram_gb, m.ram_slots * p.max_module_gb) : p.controller_max_ram_gb;
        const vary = phys < m.official_max_ram_gb ? " ⚠随 CPU SKU 而异, 见平台备注" : "";
        return `${phys}GB${p.max_module_gb ? ` (${m.ram_slots}槽×单条${p.max_module_gb}GB, 控制器 ${p.controller_max_ram_gb}GB${vary})` : ""}`;
      })()} · ${esc(m.ram_type || "")} · 插槽 ×${m.ram_slots}${
        m.ram_slots > 0
          ? ' · <span style="color:var(--official)">插槽式, 可自行升级</span>'
          : ' · <span style="color:var(--experimental)">焊接, 常规不可升级</span>'}</div></div>
      ${d.platform ? `<div><div class="k">平台 (内存控制器)</div><div class="v">${esc(d.platform.name)} · ${esc(d.platform.memory_controller)}</div></div>` : ""}
      <div><div class="k">存储接口</div><div class="v">${esc(m.storage_interface)}</div></div>
      ${m.stock_gpu ? `<div><div class="k">原装显卡</div><div class="v">${esc(m.stock_gpu)} · ${
        m.stock_gpu_metal ? '<span style="color:var(--official)">Metal ✓</span>'
                          : '<span style="color:var(--experimental)">非 Metal</span>'}</div></div>` : ""}
      <div><div class="k">NVMe 引导</div><div class="v">${NVME_LABEL[m.nvme_bootable] || esc(m.nvme_bootable)}</div></div>
      <div><div class="k">官方最高系统</div><div class="v">${esc(m.max_macos || "未知")}</div></div>
    </div>
    ${d.cpu_options.length ? `<div style="margin-top:14px">
      <div class="k" style="font-size:11px;color:var(--muted)">CPU 配置档 (${d.cpu_options.length} 档; 主频/核数源自 Apple 页面, 型号编号经 EveryMac 核对)</div>
      ${d.cpu_options.map(c => `<div style="font-size:13px;padding:2px 0">
        <span class="tag">${c.config_type === "standard" ? "标配" : "选配"}</span>
        ${c.ghz}GHz ${c.cores}核 — ${esc(c.cpu_model)}${c.notes ? ` <span style="color:var(--muted)">(${esc(c.notes)})</span>` : ""}
      </div>`).join("")}</div>` : ""}
    ${d.platform && d.platform.notes && m.ram_slots > 0 ? `<div class="notes" style="margin-top:10px;font-size:12px">${esc(d.platform.notes)}</div>` : ""}
    <div style="margin-top:12px" class="sources">
      <a href="${esc(m.apple_spec_url)}" target="_blank" rel="noopener">↗ Apple 官方规格页 (来源)</a>
      ${d.platform && d.platform.controller_source_url ? `<a href="${esc(d.platform.controller_source_url)}" target="_blank" rel="noopener">↗ Intel ARK (控制器上限来源)</a>` : ""}
    </div>
  </div>
  <div class="card"><h2 style="font-size:15px">升级建议 (推荐引擎)</h2>
    <div class="adv-controls">
      <label style="font-size:12px;color:var(--muted)">用途</label>
      <select id="adv-usage" onchange="toggleWildRisk(this.value)">${USAGE_OPTS.filter(u => (u !== "野路子" || d.wild_available) && (u !== "黑苹果续命" || d.oclp_applicable) && (u !== "秀肌肉" || d.flex_available)).map(u => `<option>${u}</option>`).join("")}</select>
      <span id="adv-risk-wrap"><label style="font-size:12px;color:var(--muted)">风险偏好</label>
      <select id="adv-risk">${RISK_OPTS.map(([v, t]) =>
        `<option value="${v}"${v === "community" ? " selected" : ""}>${t}</option>`).join("")}</select></span>
      <span id="adv-risk-fixed" style="display:none;font-size:12px;color:var(--experimental)">风险: 拉满 (野路子固定)</span>
      <span id="adv-target-wrap" style="display:none"><label style="font-size:12px;color:var(--muted)">目标系统</label>
      <select id="adv-target">${(d.oclp_targets || []).map((t, i, a) =>
        `<option value="${t.version}"${i === a.length - 1 ? " selected" : ""}>macOS ${t.version} (${t.name})</option>`).join("")}</select></span>
      <button onclick="runAdvise()">生成推荐</button>
    </div>
    <div id="advise-result"></div>
  </div>
  ${d.ports.length ? `<div class="card"><h2 style="font-size:15px">扩展端口 / 总线</h2>
    ${d.ports.map(p => `<div class="entry" style="margin-top:10px">
      <div class="title">[${PORT_LABEL[p.port_type] || esc(p.port_type)}] ${esc(p.spec)}${p.count > 1 ? ` ×${p.count}` : ""}</div>
      ${p.notes ? `<div class="notes">${esc(p.notes)}</div>` : ""}
      <div class="sources"><a href="${esc(p.source_url)}" target="_blank" rel="noopener">↗ ${esc(p.source_url)}</a></div>
    </div>`).join("")}</div>` : ""}
  ${d.constraints.length ? `<div class="card"><h2 style="font-size:15px">固件 / 系统约束</h2>
    ${d.constraints.map(c => `<div class="entry l-${c.confidence_level}" style="margin-top:10px">
      <div class="title">[${CONSTRAINT_LABEL[c.constraint_type] || esc(c.constraint_type)}] <span class="badge ${c.confidence_level}">${LAYER_LABEL[c.confidence_level]}</span>
        <span class="tag">作用域: ${c.scope === "global" ? "全系统" : c.scope === "platform" ? "整个平台" : "本机型"}</span></div>
      <div class="notes">${esc(c.description)}</div>
      ${c.affected_versions ? `<div class="tags"><span class="tag">影响范围: ${esc(c.affected_versions)}</span></div>` : ""}
      ${sourcesHtml(c)}
    </div>`).join("")}</div>` : ""}
  ${d.gpu_archs.length ? `<div class="card"><details><summary style="font-size:14px;font-weight:600;cursor:pointer">本机显卡升级路径: ${d.gpu_paths.map(esc).join(" / ")} — 展开查看 GPU 架构 × macOS 驱动区间</summary>
    ${d.gpu_archs.map(g => `<div class="entry" style="margin-top:10px">
      <div class="title">${esc(g.vendor)} ${esc(g.arch)}${g.path_class === "unorthodox" ? ` <span class="badge" style="color:#b02a37;background:#fdecee">野路子</span>` : ""}${g.example_cards ? ` <span style="color:var(--muted);font-weight:400">(${esc(g.example_cards)})</span>` : ""}</div>
      <div class="tags"><span class="tag">原生驱动: ${esc(g.macos_native)}</span>
        ${g.macos_patched ? `<span class="tag">补丁后: ${esc(g.macos_patched)}</span>` : ""}
        ${g.metal_support ? `<span class="tag">Metal: ${esc(g.metal_support)}</span>` : ""}
        ${g.entry_cards ? `<span class="tag">门槛卡: ${esc(g.entry_cards)}</span>` : ""}
        ${g.flagship_cards ? `<span class="tag">拉满卡: ${esc(g.flagship_cards)}</span>` : ""}</div>
      ${g.notes ? `<div class="notes">${esc(g.notes)}</div>` : ""}
      ${sourcesHtml(g)}
    </div>`).join("")}</details></div>` : ""}
  <div class="card"><h2 style="font-size:15px">升级方案</h2>`;
  let total = 0;
  for (const layer of ["official", "community_tested", "experimental"]) {
    const rows = d.compatibility[layer];
    html += `<h3 class="layer"><span class="badge ${layer}">${LAYER_LABEL[layer]}</span></h3>`;
    if (!rows.length) { html += `<div class="empty-layer">该层暂无收录</div>`; continue; }
    total += rows.length;
    html += rows.map(r => entryHtml(r, layer)).join("");
  }
  if (!total) html += `<div class="empty-layer" style="margin-top:8px">该机型暂未收录升级方案 (数据库仍在扩充)</div>`;
  html += `</div>`;
  if (d.conflicts.length) {
    html += `<div class="card"><h2 style="font-size:15px" class="warn-title">⚠ 已知冲突 / 风险案例</h2>`;
    html += d.conflicts.map(k => `<div class="entry conflict" style="margin-top:10px">
      <div class="title">${esc(k.is_generic ? "(泛型) " + k.interface :
        [k.manufacturer, k.part_model].filter(Boolean).join(" "))}
        <span class="tag">严重度: ${SEVERITY_LABEL[k.severity] || esc(k.severity)}</span></div>
      <div class="notes">${esc(k.description)}</div>
      ${k.workaround ? `<div class="notes">规避: ${esc(k.workaround)}</div>` : ""}
      ${sourcesHtml(k)}</div>`).join("");
    html += `</div>`;
  }
  document.getElementById("detail").innerHTML = html;
  document.getElementById("detail").scrollTop = 0;
}

function toggleWildRisk(usage) {
  const wild = usage === "野路子";
  document.getElementById("adv-risk-wrap").style.display = wild ? "none" : "";
  document.getElementById("adv-risk-fixed").style.display = wild ? "" : "none";
  document.getElementById("adv-target-wrap").style.display = usage === "黑苹果续命" ? "" : "none";
}

async function runAdvise() {
  const usage = document.getElementById("adv-usage").value;
  const risk = usage === "野路子" ? "experimental" : document.getElementById("adv-risk").value;
  const targetSel = document.getElementById("adv-target");
  const target = usage === "黑苹果续命" && targetSel && targetSel.value ? `&target=${encodeURIComponent(targetSel.value)}` : "";
  const el = document.getElementById("advise-result");
  el.innerHTML = `<div style="color:var(--muted);font-size:13px">计算中…</div>`;
  const resp = await fetch(`/api/advise?id=${encodeURIComponent(activeId)}&usage=${encodeURIComponent(usage)}&risk=${risk}${target}`);
  if (!resp.ok) { el.innerHTML = `<div class="notes">请求失败</div>`; return; }
  const d = await resp.json();
  let html = `<h3 class="layer" style="margin-top:10px">瓶颈诊断</h3>
    ${d.diagnosis.map(x => `<div style="font-size:13px;padding:2px 0">• ${esc(x)}</div>`).join("")}`;
  if (!d.recommendations.length) {
    html += `<div class="empty-layer" style="margin-top:10px">${esc(d.empty_hint || "当前风险偏好下没有可推荐项")}</div>`;
  } else {
    html += `<h3 class="layer" style="margin-top:14px">推荐 (${d.recommendations.length} 条, 按用途权重排序)</h3>`;
    html += d.recommendations.map((r, i) => `<div class="entry l-${r.layer}">
      <div class="title">${i + 1}. <span class="badge ${r.layer}">${LAYER_LABEL[r.layer]}</span>${r.wild_exclusive ? ` <span class="badge" style="color:#b02a37;background:#fdecee">野路子独有</span>` : ""}${r.factory_part ? ` <span class="badge official">原厂选配同款</span>` : ""} ${esc(r.title)}</div>
      ${r.why ? `<div style="font-size:12px;color:var(--muted);margin:3px 0">为什么: ${esc(r.why)}</div>` : ""}
      <div class="tags">
        ${r.result ? `<span class="tag">${RESULT_LABEL[r.result] || esc(r.result)}</span>` : ""}
        ${r.max_working_capacity_gb ? `<span class="tag">${r.layer === "official" ? "官方上限" : "实测上限"} ${r.max_working_capacity_gb}GB</span>` : ""}
        ${r.verified_macos_versions ? `<span class="tag">验证系统: ${esc(r.verified_macos_versions)}</span>` : ""}
        ${r.requires_adapter ? `<span class="tag">需转接卡</span>` : ""}
      </div>
      ${r.notes ? `<div class="notes">${esc(r.notes)}</div>` : ""}
      ${r.warnings.map(w => `<div class="notes" style="background:#fffafa;border:1px solid #f1c0c5">⚠ ${esc(w.text)}</div>`).join("")}
      <div class="sources">${r.sources.map(u => `<a href="${esc(u)}" target="_blank" rel="noopener">↗ ${esc(u)}</a>`).join("")}</div>
    </div>`).join("");
  }
  (d.mutual_warnings || []).forEach(w => {
    html += `<div class="notes" style="background:#fffafa;border:1px solid #f1c0c5;margin-top:8px">⚠⚠ ${esc(w)}</div>`;
  });
  if (d.irrelevant_skipped) {
    html += `<div style="font-size:12px;color:var(--muted);margin-top:6px">有 ${d.irrelevant_skipped} 条与该用途无关的条目已省略</div>`;
  }
  if (d.hidden_by_risk) {
    html += `<div style="font-size:12px;color:var(--muted);margin-top:6px">有 ${d.hidden_by_risk} 条更低可信度的方案被当前风险偏好隐藏</div>`;
  }
  html += `<div style="font-size:11px;color:var(--muted);margin-top:8px">${esc(d.disclaimer)}</div>`;
  el.innerHTML = html;
}

(async () => {
  allModels = await (await fetch("/api/models")).json();
  renderList();
  document.getElementById("search").addEventListener("input", renderList);
})();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json; charset=utf-8", status=200):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send(PAGE, "text/html; charset=utf-8")
        elif parsed.path == "/api/models":
            self._send(json.dumps(api_models(), ensure_ascii=False))
        elif parsed.path == "/api/model":
            ident = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            detail = api_model_detail(ident)
            if detail is None:
                self._send(json.dumps({"error": "not found"}), status=404)
            else:
                self._send(json.dumps(detail, ensure_ascii=False))
        elif parsed.path == "/api/advise":
            q = urllib.parse.parse_qs(parsed.query)
            ident = q.get("id", [""])[0]
            usage = q.get("usage", [""])[0]
            risk = q.get("risk", ["community"])[0]
            if usage not in advisor.USAGES or risk not in advisor.RISKS:
                self._send(json.dumps({"error": "参数无效"}), status=400)
                return
            target = q.get("target", [None])[0] or None
            result = advisor.advise(ident, usage, risk, target=target)
            if result is None:
                self._send(json.dumps({"error": "not found"}), status=404)
            else:
                self._send(json.dumps(result, ensure_ascii=False))
        else:
            self._send(json.dumps({"error": "not found"}), status=404)

    def log_message(self, fmt, *args):
        pass  # 静默访问日志


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"数据库不存在, 先运行 scripts/init_db.py 和采集脚本: {DB_PATH}")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"x86 Mac 硬件升级顾问 已启动: {url}  (Ctrl+C 退出)")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
