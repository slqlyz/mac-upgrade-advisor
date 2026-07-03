#!/usr/bin/env python3
"""引擎不变量自检: 把历轮评审确立的设计规则写成断言, 全库扫描。

不变量清单 (违反任何一条 = 逻辑错误):
  I1  引擎对 38 机型 × 全用途 × 全风险不崩溃
  I2  风险单调性: official ⊆ community ⊆ experimental (按类别覆盖)
  I3  野路子 = 秀肌肉严格超集 (秀肌肉每个条目标题都在野路子中)
  I4  原厂恒绿: factory_part 条目在任何风险级 layer 都是 official
  I5  仅官方风险级下所有条目 layer == official
  I6  笔记本: 无秀肌肉档、任何用途无 eGPU 条目
  I7  Tahoe 机型 / OCLP 无目标机型: 无黑苹果续命
  I8  每条推荐 sources 非空且为 http(s)
  I9  wild_exclusive 只出现在野路子
  I10 系统版本警告自洽: 验证区间覆盖目标时不得报警
  I11 数据: 非 Metal 关键词卡 ↔ stock_gpu_metal=0; community 行 corroboration≥2;
      corroboration == 1+len(extra_sources); 每机型 gpu_options 有标配档
"""
import json, re, sys
sys.path.insert(0, "scripts")
from advisor import advise, USAGES, RISKS, parse_versions
import sqlite3

conn = sqlite3.connect("data/mac_upgrade.db")
conn.row_factory = sqlite3.Row
models = [r["model_identifier"] for r in conn.execute("SELECT model_identifier FROM models ORDER BY 1")]
errors = []

def E(tag, msg):
    errors.append(f"[{tag}] {msg}")

results = {}
for ident in models:
    for usage in USAGES:
        for risk in RISKS:
            try:
                r = advise(ident, usage, risk)
            except Exception as e:
                E("I1", f"{ident}×{usage}×{risk}: 崩溃 {type(e).__name__}: {e}")
                continue
            if r is None or r.get("error"):
                continue
            results[(ident, usage, risk)] = r
            for rec in r["recommendations"]:
                # 野路子恒为拉满风险 (用户裁定), I5 不适用
                if risk == "official" and usage != "野路子" and rec["layer"] != "official":
                    E("I5", f"{ident}×{usage}: 仅官方风险下出现 {rec['layer']} 层 [{rec['title'][:40]}]")
                if rec.get("factory_part") and rec["layer"] != "official":
                    E("I4", f"{ident}×{usage}×{risk}: 原厂件 {rec['title'][:40]} 层为 {rec['layer']}")
                if not rec["sources"] or not all(str(u).startswith("http") for u in rec["sources"]):
                    E("I8", f"{ident}×{usage}×{risk}: 来源缺失/非法 [{rec['title'][:40]}]")
                if rec.get("wild_exclusive") and usage != "野路子":
                    E("I9", f"{ident}×{usage}: 非野路子出现独有标记 [{rec['title'][:40]}]")
                if "eGPU" in rec["title"]:
                    fam = conn.execute("SELECT family FROM models WHERE model_identifier=?",
                                       (ident,)).fetchone()["family"]
                    if fam.startswith("MacBook"):
                        E("I6", f"{ident}×{usage}: 笔记本出现 eGPU 条目")
                win = parse_versions(rec.get("verified_macos_versions"))
                tgt = r.get("target_macos")
                if win and tgt and win[1] is not None and win[1] >= tgt[0]:
                    if any(w["kind"] == "os" for w in rec["warnings"]):
                        E("I10", f"{ident}×{usage}×{risk}: 区间覆盖目标仍报警 [{rec['title'][:40]}]")

# I2 风险单调性 (类别覆盖) & I3 超集
for ident in models:
    for usage in USAGES:
        cats = {}
        for risk in RISKS:
            r = results.get((ident, usage, risk))
            if r:
                cats[risk] = {x["category"] for x in r["recommendations"]}
        if "official" in cats and "community" in cats and not cats["official"] <= cats["community"]:
            E("I2", f"{ident}×{usage}: official 类别 {cats['official'] - cats['community']} 在 community 消失")
        if "community" in cats and "experimental" in cats and not cats["community"] <= cats["experimental"]:
            E("I2", f"{ident}×{usage}: community 类别 {cats['community'] - cats['experimental']} 在 experimental 消失")
    flex = results.get((ident, "秀肌肉", "experimental"))
    wild = results.get((ident, "野路子", "experimental"))
    if flex and wild and flex["recommendations"] and wild["recommendations"]:
        ft = {x["title"] for x in flex["recommendations"]}
        wt = {x["title"] for x in wild["recommendations"]}
        missing = ft - wt
        if missing:
            E("I3", f"{ident}: 秀肌肉条目在野路子缺失: {sorted(missing)[:2]}")

# I6b/I7 档位可用性
for ident in models:
    row = conn.execute("SELECT family, max_macos FROM models WHERE model_identifier=?", (ident,)).fetchone()
    r = results.get((ident, "秀肌肉", "community"))
    if row["family"].startswith("MacBook") and r and r["recommendations"]:
        E("I6", f"{ident}: 笔记本秀肌肉有内容")
    r = results.get((ident, "黑苹果续命", "community"))
    if "Tahoe" in (row["max_macos"] or "") and r and r["recommendations"]:
        E("I7", f"{ident}: Tahoe 机型黑苹果续命有内容")

# I11 数据检查
NONMETAL_KW = ["6970M", "6750M", "6770M", "6490M", "6630M", "HD 5770", "HD 5870",
               "GT 120", "GT 330M", "HD Graphics 3000", "HD 3000", "HD 4870",
               "320M", "HD 4850", "HD 5670", "HD 5750", "Tesla", "TeraScale"]
for m in conn.execute("SELECT * FROM models"):
    sg = m["stock_gpu"] or ""
    is_nonmetal_kw = any(k in sg for k in NONMETAL_KW) and "4000" not in sg
    if is_nonmetal_kw and m["stock_gpu_metal"] == 1:
        E("I11", f"{m['model_identifier']}: 非 Metal 关键词卡但 metal=1 ({sg[:40]})")
    if not is_nonmetal_kw and m["stock_gpu_metal"] == 0 and sg:
        E("I11", f"{m['model_identifier']}: metal=0 但无非 Metal 关键词 ({sg[:40]})")
    n_std = conn.execute("SELECT COUNT(*) FROM gpu_options WHERE model_id=? AND config_type='standard'",
                         (m["id"],)).fetchone()[0]
    if n_std == 0:
        E("I11", f"{m['model_identifier']}: 显卡档无标配行")
for c in conn.execute("SELECT c.*, m.model_identifier mi FROM compatibility c JOIN models m ON m.id=c.model_id"):
    extras = json.loads(c["extra_source_urls"] or "[]")
    if c["confidence_level"] == "community_tested" and c["corroboration_count"] < 2:
        E("I11", f"{c['mi']} 实证: community 但 corroboration={c['corroboration_count']}")
    if c["corroboration_count"] != 1 + len(extras):
        E("I11", f"{c['mi']} 实证 [{(c['notes'] or '')[:25]}]: corroboration={c['corroboration_count']} != 1+{len(extras)} 个来源")

print(f"扫描: {len(results)} 个 机型×用途×风险 组合")
if errors:
    print(f"\n发现 {len(errors)} 处违反不变量:")
    for e in errors[:40]:
        print(" ", e)
else:
    print("全部不变量通过")
conn.close()
sys.exit(1 if errors else 0)
