#!/usr/bin/env python3
"""升级方案侦察器: 自动搜寻候选实证 (特别是野路子), 人只做收录裁决。

流程:
  1. 从数据库生成搜索任务: 实证最少的机型 × 改装关键词 + 固定愿望单
     (mini 2018 换芯 / 17吋杂交 / 颗粒加焊 等) + 固定猎场 RSS (r/modmac)
  2. DuckDuckGo 检索 (零依赖, POST html 端点), 礼貌限速
  3. 过滤评分: 可信域名加权 (macrumors/reddit/lowendmac/insanelymac/
     tonymacx86/ifixit/chiphell/bilibili), 改装关键词计数
  4. 去重: 已入库来源 (compatibility 全部 URL) + 历史已展示 (data/scout/seen.json)
  5. 产出候选报告 → 你裁决后我按规则入库 (本脚本绝不直接写库)

用法:
    python3 scripts/scout.py --auto 6          # 自动挑 6 个数据最薄的机型侦察
    python3 scripts/scout.py --model iMacPro1,1  # 定向侦察某机型
    python3 scripts/scout.py --wishlist        # 只跑固定愿望单
"""
import argparse, json, re, sqlite3, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "mac_upgrade.db"
SEEN = ROOT / "data" / "scout" / "seen.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
      "Content-Type": "application/x-www-form-urlencoded"}

TRUST = {"forums.macrumors.com": 3, "reddit.com": 3, "lowendmac.com": 3,
         "insanelymac.com": 2, "tonymacx86.com": 2, "ifixit.com": 2,
         "chiphell.com": 3, "bilibili.com": 2, "youtube.com": 1, "egpu.io": 2,
         "tinkerdifferent.com": 2, "51nb.com": 2}
MOD_KW = ["reball", "swap", "mod", "solder", "quad", "vbios", "coreboot", "flash",
          "upgrade", "换芯", "爆改", "加焊", "改装", "颗粒", "魔改"]
NOISE = ["ebay.com", "amazon.", "aliexpress", "walmart", "/shop/", "coretekcomputers"]

WISHLIST = [
    ("Macmini8,1", "mac mini 2018 CPU swap 9980HK BGA 改装"),
    ("Macmini8,1", "mac mini 2018 换 i9 9980HK chiphell"),
    ("MacBookPro8,3", "macbook pro 17 2011 ivy bridge coreboot hybrid mod"),
    ("iMacPro1,1", "imac pro 2017 xeon CPU upgrade swap W-2191B"),
    ("*", "macbook 内存 颗粒 加焊 扩容 16g 32g"),
    ("*", "dosdude1 mod reball macbook new"),
]
RSS_HUNTS = [("r/modmac", "https://www.reddit.com/r/modmac/new/.rss")]


def ddg(query):
    data = urllib.parse.urlencode({"q": query}).encode()
    req = urllib.request.Request("https://html.duckduckgo.com/html/", data=data, headers=UA)
    h = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    out = []
    for m in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', h) or []:
        out.append((m.group(1), re.sub(r"<[^>]+>", "", m.group(2))))
    if not out:  # 兜底: 宽松抓外链
        for m in re.finditer(r'href="(https?://[^"]+)"[^>]*>([^<]{15,120})<', h):
            if "duckduckgo" not in m.group(1):
                out.append((m.group(1), m.group(2)))
    return out


def score(url, title):
    dom = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    if any(n in url for n in NOISE):
        return -1
    s = 0
    for d, w in TRUST.items():
        if dom.endswith(d):
            s += w
    tl = title.lower()
    s += sum(1 for k in MOD_KW if k in tl or k in url.lower())
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--auto", type=int, default=0, help="自动挑 N 个数据最薄的机型")
    ap.add_argument("--model", help="定向侦察机型标识")
    ap.add_argument("--wishlist", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    known = {r[0] for r in conn.execute("SELECT source_url FROM compatibility")}
    for r in conn.execute("SELECT extra_source_urls FROM compatibility"):
        known.update(json.loads(r[0] or "[]"))
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()

    tasks = []
    if args.model:
        m = conn.execute("SELECT * FROM models WHERE model_identifier=?", (args.model,)).fetchone()
        name = m["model_name"]
        tasks += [(args.model, f"{args.model} CPU swap mod reball"),
                  (args.model, f'"{name}" upgrade mod 改装'),
                  (args.model, f"{args.model} ram solder upgrade 颗粒")]
    if args.auto:
        thin = conn.execute("""SELECT m.model_identifier id2, m.model_name,
            (SELECT COUNT(*) FROM compatibility c WHERE c.model_id=m.id) n
            FROM models m ORDER BY n, m.release_year DESC LIMIT ?""", (args.auto,)).fetchall()
        for t in thin:
            tasks.append((t["id2"], f'{t["id2"]} OR "{t["model_name"]}" upgrade mod swap'))
    if args.wishlist or not tasks:
        tasks += WISHLIST

    print(f"侦察任务 {len(tasks)} 条 (已知来源 {len(known)}, 历史已阅 {len(seen)})\n")
    candidates = []
    for target, q in tasks:
        try:
            hits = ddg(q)
        except Exception as e:
            print(f"  [搜索失败] {q[:50]}: {type(e).__name__}")
            continue
        fresh = []
        for u, t in hits:
            u = u.split("&rut=")[0]
            if u in known or u in seen:
                continue
            sc = score(u, t)
            if sc >= 3:
                fresh.append((sc, u, t.strip()))
                seen.add(u)
        fresh.sort(reverse=True)
        if fresh:
            print(f"◆ [{target}] {q[:60]}")
            for sc, u, t in fresh[:4]:
                print(f"   ({sc}) {t[:75]}\n        {u[:100]}")
                candidates.append({"target": target, "score": sc, "title": t, "url": u, "query": q})
            print()
        time.sleep(2)

    # 固定猎场 RSS
    for name, feed in RSS_HUNTS:
        try:
            req = urllib.request.Request(feed, headers={"User-Agent": UA["User-Agent"]})
            h = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
            entries = re.findall(r"<entry>.*?<title>(.*?)</title>.*?<link href=\"([^\"]+)\"", h, re.S)
            fresh = [(t, u) for t, u in entries if u not in seen and u not in known
                     and "Lounge" not in t]
            if fresh:
                print(f"◆ [猎场 {name}]")
                for t, u in fresh[:5]:
                    print(f"   {t[:75]}\n        {u[:100]}")
                    candidates.append({"target": "*", "score": 3, "title": t, "url": u, "query": name})
                    seen.add(u)
        except Exception as e:
            print(f"  [猎场失败] {name}: {type(e).__name__}")

    SEEN.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=1))
    out = ROOT / "data" / "scout" / "candidates.json"
    prev = json.loads(out.read_text()) if out.exists() else []
    out.write_text(json.dumps(prev + candidates, ensure_ascii=False, indent=1))
    print(f"\n本轮候选 {len(candidates)} 条 → {out.relative_to(ROOT)}")
    print("裁决方式: 把看中的 URL 发给我, 按分层规则核验入库; 无价值的不用管 (已记入已阅, 不再打扰)")
    conn.close()


if __name__ == "__main__":
    main()
