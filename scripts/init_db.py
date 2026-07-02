#!/usr/bin/env python3
"""初始化数据库并自检 schema 约束。

用法:
    python3 scripts/init_db.py            # 建库 data/mac_upgrade.db
    python3 scripts/init_db.py --check    # 建库后跑约束自检 (在内存副本上, 不污染正式库)

零依赖, 仅用标准库 sqlite3。
"""

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema.sql"
DB_PATH = ROOT / "data" / "mac_upgrade.db"


def create_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def self_check():
    """在内存库上验证枚举约束和触发器确实拦截脏数据。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    conn.execute(
        """INSERT INTO models (model_name, model_identifier, release_year, family,
               cpu_model, official_max_ram_gb, storage_interface, apple_spec_url)
           VALUES ('测试机型', 'TestMac1,1', 2015, 'MacBook Pro',
               'Intel Core i7', 16, 'proprietary-NVMe', 'https://support.apple.com/test')"""
    )
    conn.execute(
        """INSERT INTO components (category, interface, capacity_gb, is_generic)
           VALUES ('ram', 'DDR3-SODIMM', 16, 1)"""
    )

    def expect_reject(desc, sql, params=()):
        try:
            conn.execute(sql, params)
        except sqlite3.IntegrityError as e:
            print(f"  ✓ 正确拦截: {desc}  ({e})")
            return True
        print(f"  ✗ 未拦截: {desc}")
        return False

    def expect_accept(desc, sql, params=()):
        try:
            conn.execute(sql, params)
            print(f"  ✓ 正常写入: {desc}")
            return True
        except sqlite3.IntegrityError as e:
            print(f"  ✗ 误拦截: {desc}  ({e})")
            return False

    print("约束自检:")
    ok = all([
        expect_reject(
            "official 条目使用论坛来源",
            """INSERT INTO compatibility (model_id, component_id, confidence_level,
                   source_url, source_type)
               VALUES (1, 1, 'official', 'https://forums.macrumors.com/x', 'macrumors_forum')""",
        ),
        expect_reject(
            "community_tested 但只有 1 个来源",
            """INSERT INTO compatibility (model_id, component_id, confidence_level,
                   source_url, source_type, corroboration_count)
               VALUES (1, 1, 'community_tested', 'https://forums.macrumors.com/x',
                   'macrumors_forum', 1)""",
        ),
        expect_reject(
            "非法 confidence_level 枚举值",
            """INSERT INTO compatibility (model_id, component_id, confidence_level,
                   source_url, source_type)
               VALUES (1, 1, 'rumor', 'https://example.com', 'reddit')""",
        ),
        expect_reject(
            "compatibility 缺 source_url",
            """INSERT INTO compatibility (model_id, component_id, confidence_level,
                   source_url, source_type)
               VALUES (1, 1, 'experimental', NULL, 'reddit')""",
        ),
        expect_reject(
            "外键: 引用不存在的机型",
            """INSERT INTO compatibility (model_id, component_id, confidence_level,
                   source_url, source_type)
               VALUES (999, 1, 'experimental', 'https://example.com', 'reddit')""",
        ),
        expect_accept(
            "official 条目 + apple_support 来源",
            """INSERT INTO compatibility (model_id, component_id, confidence_level,
                   source_url, source_type)
               VALUES (1, 1, 'official', 'https://support.apple.com/test', 'apple_support')""",
        ),
        expect_accept(
            "community_tested + 2 个独立来源",
            """INSERT INTO compatibility (model_id, component_id, confidence_level,
                   source_url, source_type, corroboration_count, extra_source_urls,
                   verified_macos_versions, notes)
               VALUES (1, 1, 'community_tested', 'https://forums.macrumors.com/x',
                   'macrumors_forum', 2, '["https://reddit.com/r/mac/y"]',
                   '10.15–12.6', '需 BootROM 更新')""",
        ),
        expect_accept(
            "experimental 单来源孤例",
            """INSERT INTO compatibility (model_id, component_id, confidence_level,
                   source_url, source_type)
               VALUES (1, 1, 'experimental', 'https://reddit.com/r/mac/z', 'reddit')""",
        ),
    ])
    conn.close()
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="建库后运行约束自检")
    args = parser.parse_args()

    conn = create_db(DB_PATH)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    conn.close()
    print(f"数据库已创建: {DB_PATH}")
    print(f"表: {', '.join(tables)}")

    if args.check and not self_check():
        sys.exit(1)


if __name__ == "__main__":
    main()
