#!/usr/bin/env python3
"""离线自举: 从种子 JSON 重建数据库, 零联网、零命令行。
分发场景 (朋友解压 zip 后没有 data/mac_upgrade.db) 由 serve.py 自动调用。"""
import subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "mac_upgrade.db"
PY = sys.executable or "python3"


def build(verbose=True):
    steps = [
        (["scripts/init_db.py"], "建库"),
        (["scripts/collect_official.py", "--offline"], "官方规格"),
        (["scripts/collect_platform.py"], "平台层"),
        (["scripts/collect_community.py"], "社区方案"),
    ]
    for args, label in steps:
        if verbose:
            print(f"  · {label} …", flush=True)
        r = subprocess.run([PY] + args, cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            sys.stderr.write(f"[自举失败] {label}:\n{r.stdout}\n{r.stderr}\n")
            return False
    return True


def ensure_db(verbose=True):
    """库不存在则重建; 已存在直接返回。"""
    if DB.exists():
        return True
    if verbose:
        print("首次运行: 正在从种子数据构建本地数据库 (无需联网, 约几秒)…", flush=True)
    ok = build(verbose)
    if ok and verbose:
        print("构建完成。\n", flush=True)
    return ok


if __name__ == "__main__":
    sys.exit(0 if ensure_db() else 1)
