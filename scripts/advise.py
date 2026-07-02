#!/usr/bin/env python3
"""推荐引擎 CLI (第三阶段)。

用法:
    python3 scripts/advise.py MacBookPro11,4 --usage 视频剪辑 --risk community
    python3 scripts/advise.py iMac12,2 --usage 跑虚拟机 --risk experimental

用途: 轻度日用 / 黑苹果续命 / 秀肌肉 / 野路子 (支持简写模糊匹配)
风险: official (仅官方) / community (接受社区验证, 默认) / experimental (接受实验+理论推导)
"""

import argparse
import sys

from advisor import USAGES, RISKS, advise

LAYER_LABEL = {
    "official": "官方支持",
    "community_tested": "社区验证 (≥2 独立来源)",
    "experimental": "实验性 (孤例, 风险自担)",
    "derived": "理论推导 (无实证)",
}
RESULT_LABEL = {"works": "可用", "works_with_caveats": "可用但有注意事项",
                "partial": "部分可用", "failed": "失败案例"}


def match_usage(text):
    hits = [u for u in USAGES if text in u or u in text]
    if len(hits) == 1:
        return hits[0]
    sys.exit(f"用途 {text!r} 不明确, 可选: {' / '.join(USAGES)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", help="机型标识, 如 MacBookPro11,4")
    parser.add_argument("--usage", required=True, help="用途 (可简写, 如 剪辑/虚拟机/黑苹果)")
    parser.add_argument("--risk", default="community", choices=RISKS)
    parser.add_argument("--target", default=None,
                        help="黑苹果续命的目标系统版本 (如 12/15), 缺省取 OCLP 支持的最高版")
    args = parser.parse_args()

    usage = match_usage(args.usage)
    if usage == "野路子" and args.risk != "experimental":
        print("(野路子默认拉满风险, --risk 参数已忽略)\n")
    result = advise(args.model, usage, args.risk, target=args.target)
    if result is None:
        sys.exit(f"未找到机型: {args.model} (用 scripts/lookup.py --list 查看)")
    if result.get("error"):
        sys.exit(result["error"])
    if usage == "黑苹果续命" and result.get("target_options"):
        opts = ", ".join(f"{o['version']} ({o['name']})" for o in result["target_options"])
        print(f"可选目标系统: {opts}  (--target 指定)\n")

    print(f"═══ 升级建议: {result['model_name']} ({result['model_identifier']}) ═══")
    risk_label = "拉满 (野路子固定)" if usage == "野路子" else result["risk"]
    print(f"用途: {result['usage']}  |  风险偏好: {risk_label}\n")

    if result.get("target_macos"):
        print(f"目标系统: macOS {result['target_macos'][1]} (校验各方案验证区间是否覆盖)\n")
    print("瓶颈诊断:")
    for d in result["diagnosis"]:
        print(f"  • {d}")
    print()

    if not result["recommendations"]:
        print(result.get("empty_hint", "当前风险偏好下没有可推荐项。"))
    else:
        print(f"推荐 ({len(result['recommendations'])} 条, 按用途权重排序, 层级硬性分界):\n")
        for i, r in enumerate(result["recommendations"], 1):
            wild = "【野路子独有】" if r.get("wild_exclusive") else ""
            fac = "【原厂选配同款】" if r.get("factory_part") else ""
            print(f"  {i}. [{LAYER_LABEL[r['layer']]}]{wild}{fac} {r['title']}")
            if r["why"]:
                print(f"     为什么: {r['why']}")
            details = []
            if r["result"]:
                details.append(RESULT_LABEL.get(r["result"], r["result"]))
            if r["max_working_capacity_gb"]:
                cap_label = "官方上限" if r["layer"] == "official" else "实测上限"
                details.append(f"{cap_label} {r['max_working_capacity_gb']}GB")
            if r["verified_macos_versions"]:
                details.append(f"验证系统 {r['verified_macos_versions']}")
            if r["requires_adapter"]:
                details.append("需转接卡")
            if details:
                print(f"     状态: {' | '.join(details)}")
            if r["notes"]:
                print(f"     注意: {r['notes']}")
            for w in r["warnings"]:
                print(f"     ⚠ {w['text']}")
            for u in r["sources"]:
                print(f"     来源: {u}")
            print()

    for w in result.get("mutual_warnings", []):
        print(f"⚠⚠ {w}")
    if result.get("irrelevant_skipped"):
        print(f"(有 {result['irrelevant_skipped']} 条与该用途无关的条目已省略)")
    if result["hidden_by_risk"]:
        print(f"(有 {result['hidden_by_risk']} 条更低可信度的方案被当前风险偏好隐藏, "
              f"提高 --risk 可见)")
    print(f"\n说明: {result['disclaimer']}")


if __name__ == "__main__":
    main()
