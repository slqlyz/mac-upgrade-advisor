-- ============================================================
-- x86 Mac 硬件升级顾问 — SQLite Schema (v1)
--
-- 设计原则:
--   1. 可信度分层: official / community_tested / experimental
--      三层数据物理上是独立记录, 永不混层。
--   2. 来源可溯源: 所有兼容性数据 source_url 必填,
--      官方规格 (models 表) 也带 apple_spec_url。
--   3. 社区方案要求多来源印证: corroboration_count >= 2
--      才能标记 community_tested (由采集脚本保证,
--      触发器兜底 official 层的来源类型)。
-- ============================================================

PRAGMA foreign_keys = ON;

-- ============================================================
-- 1. models — Mac 型号
-- ============================================================
CREATE TABLE IF NOT EXISTS models (
    id                  INTEGER PRIMARY KEY,
    model_name          TEXT NOT NULL,              -- 营销名称, 如 "MacBook Pro (Retina, 15-inch, Mid 2015)"
    model_identifier    TEXT NOT NULL UNIQUE,       -- 机型标识, 如 "MacBookPro11,4" (查询主键入口)
    board_id            TEXT,                       -- 逻辑板ID, 如 "Mac-06F11F11946D27C5" (黑苹果场景)
    release_year        INTEGER NOT NULL,
    family              TEXT NOT NULL CHECK (family IN
                          ('MacBook','MacBook Air','MacBook Pro',
                           'iMac','Mac mini','Mac Pro','iMac Pro','Xserve')),
    cpu_model           TEXT NOT NULL,              -- 如 "Intel Core i7-4870HQ"
    cpu_socket          TEXT,                       -- 'soldered' 或插槽型号如 'LGA1155'
    official_max_ram_gb INTEGER NOT NULL,           -- Apple 官方标称内存上限
    ram_type            TEXT,                       -- 如 "DDR3-1600 SO-DIMM"; 焊接记 "soldered LPDDR3"
    ram_slots           INTEGER NOT NULL DEFAULT 0, -- 0 = 焊接不可升级
    storage_interface   TEXT NOT NULL,              -- 'SATA' / 'proprietary-AHCI' / 'proprietary-NVMe' / 'PCIe-slot'
    nvme_bootable       TEXT NOT NULL DEFAULT 'no'
                          CHECK (nvme_bootable IN
                            ('native','firmware_update_required','opencore_required','no')),
    max_macos           TEXT,                       -- 官方支持的最高 macOS 版本
    apple_spec_url      TEXT NOT NULL,              -- support.apple.com 规格页 (官方数据也要可溯源)
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- 1b. cpu_options — 机型的每档 CPU 配置 (标配档 + 定制选配)
--     主频/核数以 Apple 规格页为准; 具体型号编号 (如 i5-3470S)
--     来自 EveryMac/Wikipedia 交叉核对 (Apple 页面不写编号)。
-- ============================================================
CREATE TABLE IF NOT EXISTS cpu_options (
    id          INTEGER PRIMARY KEY,
    model_id    INTEGER NOT NULL REFERENCES models(id),
    cpu_model   TEXT NOT NULL,      -- 含型号编号, 如 "Intel Xeon E5-1650 v2"
    ghz         REAL NOT NULL,
    cores       INTEGER NOT NULL,   -- 总核数 (双路机型为两颗合计)
    config_type TEXT NOT NULL CHECK (config_type IN ('standard','configurable')),
    notes       TEXT,
    UNIQUE (model_id, cpu_model, ghz)
);
CREATE INDEX IF NOT EXISTS idx_cpu_options_model ON cpu_options (model_id);

-- ============================================================
-- 1b2. gpu_options — 机型的每档显卡配置 (标配/选配), 与 cpu_options 同构
--      来源: Apple 规格页 (显卡配置逐档列在页面上, 可审计)
-- ============================================================
CREATE TABLE IF NOT EXISTS gpu_options (
    id          INTEGER PRIMARY KEY,
    model_id    INTEGER NOT NULL REFERENCES models(id),
    gpu_model   TEXT NOT NULL,      -- 'AMD Radeon HD 6970M' / 'Intel HD Graphics 4000'
    vram        TEXT,               -- '1GB' / '512MB'; 核显为 NULL
    config_type TEXT NOT NULL CHECK (config_type IN ('standard','configurable')),
    notes       TEXT,
    UNIQUE (model_id, gpu_model, vram)
);
CREATE INDEX IF NOT EXISTS idx_gpu_options_model ON gpu_options (model_id);

-- ============================================================
-- 1c. platforms — CPU 平台/内存控制器层
--     内存实际上限由内存控制器决定, 同平台共享同一条数据。
--     controller_max_ram_gb 来自 Intel ARK (以基础款 CPU 为准)。
--     注: CPU 安装方式 (焊接/插槽) 因机型而异, 留在 models.cpu_socket。
-- ============================================================
CREATE TABLE IF NOT EXISTS platforms (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL UNIQUE,   -- 'Sandy Bridge 桌面'
    cpu_microarch         TEXT NOT NULL,
    memory_controller     TEXT NOT NULL,          -- 'CPU 集成, 双通道 DDR3-1333'
    controller_max_ram_gb INTEGER,                -- 控制器上限 (ARK, 假设插满当代最大模组)
    max_module_gb         INTEGER,                -- 该世代单条最大容量; 物理可达 = min(控制器, 槽数×单条)
    controller_source_url TEXT,                   -- Intel ARK
    notes                 TEXT
);
-- models.platform_id 外键 (新库直接建, 旧库用 ALTER 迁移)

-- ============================================================
-- 1d. expansion_ports — 机型的总线/接口清单
--     "能插什么"由端口推导; 但物理匹配不等于可用,
--     还要过 hw_constraints 和 gpu_arch_support (驱动) 两道闸。
-- ============================================================
CREATE TABLE IF NOT EXISTS expansion_ports (
    id         INTEGER PRIMARY KEY,
    model_id   INTEGER NOT NULL REFERENCES models(id),
    port_type  TEXT NOT NULL CHECK (port_type IN
                 ('pcie_slot','thunderbolt','sata','sodimm_slot','mxm',
                  'apple_ssd_blade','usb','optical_bay','firewire','sd_card')),
    spec       TEXT NOT NULL,      -- 'PCIe 2.0 x16 (双宽)' / 'Thunderbolt 2 (20Gbps)'
    count      INTEGER NOT NULL DEFAULT 1,
    notes      TEXT,
    source_url TEXT NOT NULL,
    UNIQUE (model_id, port_type, spec)
);
CREATE INDEX IF NOT EXISTS idx_ports_model ON expansion_ports (model_id);

-- ============================================================
-- 1e. hw_constraints — 固件/系统级约束 (推导时的否决项/条件项)
--     scope: model=单机型 / platform=整个平台 / global=全系统 (如 eGPU 政策)
-- ============================================================
CREATE TABLE IF NOT EXISTS hw_constraints (
    id                INTEGER PRIMARY KEY,
    scope             TEXT NOT NULL CHECK (scope IN ('model','platform','global')),
    model_id          INTEGER REFERENCES models(id),
    platform_id       INTEGER REFERENCES platforms(id),
    constraint_type   TEXT NOT NULL CHECK (constraint_type IN
                        ('cpu_firmware_check','nvme_boot','egpu_support',
                         'gpu_driver','sleep_quirk','bandwidth_share','other')),
    description       TEXT NOT NULL,
    affected_versions TEXT,
    confidence_level  TEXT NOT NULL CHECK (confidence_level IN
                        ('official','community_tested','experimental')),
    source_url        TEXT NOT NULL,
    extra_source_urls TEXT,
    corroboration_count INTEGER NOT NULL DEFAULT 1,
    applicability     TEXT      -- 适用条件: has_tb3 / has_tb1_or_tb2 / bga_cpu_or_soldered_ram / NULL=恒适用
);

-- ============================================================
-- 1f. gpu_arch_support — GPU 架构 × macOS 驱动支持区间
--     驱动支持按 GPU 架构世代统一管理 (PCIe 卡 / MXM 卡 / eGPU 同用),
--     数据源: Dortania GPU 指南等 hackintosh 社区文档。
-- ============================================================
CREATE TABLE IF NOT EXISTS gpu_arch_support (
    id                INTEGER PRIMARY KEY,
    vendor            TEXT NOT NULL CHECK (vendor IN ('NVIDIA','AMD','Intel')),
    arch              TEXT NOT NULL,        -- 'Kepler' / 'Polaris' / 'RDNA 2'
    example_cards     TEXT,                 -- 'GTX 680, Quadro K4100M'
    macos_native      TEXT NOT NULL,        -- 原生驱动区间, 如 '10.7–12.x 不含 12'
    macos_patched     TEXT,                 -- 打补丁后区间, 如 '12+ 需 OCLP 根补丁'
    notes             TEXT,
    source_url        TEXT NOT NULL,
    extra_source_urls TEXT,
    corroboration_count INTEGER NOT NULL DEFAULT 1,
    UNIQUE (vendor, arch)
);

-- ============================================================
-- 1g. macos_versions — macOS 版本 × 配置要求 (与 gpu_arch_support 双向呼应)
--     metal_required: 10.14 Mojave 起强制 Metal GPU;
--     oclp_supported: OCLP 可把老机带上的版本 (黑苹果续命的目标候选)。
-- ============================================================
CREATE TABLE IF NOT EXISTS macos_versions (
    id                INTEGER PRIMARY KEY,
    version           TEXT NOT NULL UNIQUE,   -- '10.13' / '12' / '26'
    name              TEXT NOT NULL,          -- 'High Sierra' / 'Monterey' / 'Tahoe'
    release_year      INTEGER,
    metal_required    INTEGER NOT NULL DEFAULT 0,
    oclp_supported    INTEGER NOT NULL DEFAULT 0,
    notes             TEXT,
    source_url        TEXT NOT NULL,
    extra_source_urls TEXT,
    corroboration_count INTEGER NOT NULL DEFAULT 1
);

-- ============================================================
-- 2. components — 硬件组件
-- ============================================================
CREATE TABLE IF NOT EXISTS components (
    id                  INTEGER PRIMARY KEY,
    category            TEXT NOT NULL CHECK (category IN
                          ('ram','ssd','hdd','wifi_bt_card','gpu','cpu',
                           'optical_bay_caddy','adapter','battery','display','other')),
    manufacturer        TEXT,                       -- 泛型条目可空, 如 "任意 DDR3L-1600 SO-DIMM"
    part_model          TEXT,                       -- 具体型号, 如 "Samsung 970 EVO Plus"; 泛型可空
    is_generic          INTEGER NOT NULL DEFAULT 0, -- 1 = 泛型规格条目而非具体产品
    interface           TEXT NOT NULL,              -- 'DDR3-SODIMM' / 'M.2-NVMe' / 'SATA-2.5' / 'mPCIe' ...
    capacity_gb         INTEGER,                    -- 内存/存储适用
    speed_spec          TEXT,                       -- 如 "1600MHz CL11" / "PCIe 3.0 x4"
    requires_adapter    INTEGER NOT NULL DEFAULT 0, -- 如 M.2 SSD 装 2013-2015 MBP 需转接卡
    notes               TEXT,
    UNIQUE (category, manufacturer, part_model, interface, capacity_gb, speed_spec)
);

-- ============================================================
-- 3. compatibility — 型号 × 组件 兼容关系 (核心表)
--    同一 "型号×组件" 允许多条记录 (不同来源/不同可信度),
--    "官方支持 16GB" 与 "论坛实测 32GB" 是两条独立记录。
-- ============================================================
CREATE TABLE IF NOT EXISTS compatibility (
    id                  INTEGER PRIMARY KEY,
    model_id            INTEGER NOT NULL REFERENCES models(id),
    component_id        INTEGER NOT NULL REFERENCES components(id),

    confidence_level    TEXT NOT NULL CHECK (confidence_level IN
                          ('official','community_tested','experimental')),

    source_url          TEXT NOT NULL,
    source_type         TEXT NOT NULL CHECK (source_type IN
                          ('apple_support','wikipedia','everymac',
                           'macrumors_forum','reddit','dortania','other_community')),
    corroboration_count INTEGER NOT NULL DEFAULT 1, -- 独立来源互相印证的数量
    extra_source_urls   TEXT,                       -- JSON 数组, 第 2..n 个印证来源

    verified_macos_versions TEXT,                   -- 如 "10.15–12.6"; official 条目可为 NULL
    result              TEXT NOT NULL DEFAULT 'works'
                          CHECK (result IN ('works','works_with_caveats','partial','failed')),
    max_working_capacity_gb INTEGER,                -- 超规格实测上限
    notes               TEXT,                       -- 固件版本要求、睡眠掉电、总线冲突等
    date_verified       TEXT,                       -- 社区报告的验证日期 (ISO 8601)
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE (model_id, component_id, confidence_level, source_url)
);

-- 数据层兜底: official 级别条目的来源必须是官方/百科/规格库,
-- 论坛来源即使采集脚本写错也插不进去。
CREATE TRIGGER IF NOT EXISTS trg_official_source_check_insert
BEFORE INSERT ON compatibility
WHEN NEW.confidence_level = 'official'
 AND NEW.source_type NOT IN ('apple_support','wikipedia','everymac')
BEGIN
    SELECT RAISE(ABORT, 'official 级别条目的来源必须是 apple_support/wikipedia/everymac');
END;

CREATE TRIGGER IF NOT EXISTS trg_official_source_check_update
BEFORE UPDATE ON compatibility
WHEN NEW.confidence_level = 'official'
 AND NEW.source_type NOT IN ('apple_support','wikipedia','everymac')
BEGIN
    SELECT RAISE(ABORT, 'official 级别条目的来源必须是 apple_support/wikipedia/everymac');
END;

-- 兜底: community_tested 要求至少 2 个独立来源互相印证,
-- 单一孤例只能进 experimental。
CREATE TRIGGER IF NOT EXISTS trg_community_corroboration_insert
BEFORE INSERT ON compatibility
WHEN NEW.confidence_level = 'community_tested'
 AND NEW.corroboration_count < 2
BEGIN
    SELECT RAISE(ABORT, 'community_tested 要求 corroboration_count >= 2, 单一来源请标 experimental');
END;

CREATE TRIGGER IF NOT EXISTS trg_community_corroboration_update
BEFORE UPDATE ON compatibility
WHEN NEW.confidence_level = 'community_tested'
 AND NEW.corroboration_count < 2
BEGIN
    SELECT RAISE(ABORT, 'community_tested 要求 corroboration_count >= 2, 单一来源请标 experimental');
END;

-- ============================================================
-- 4. known_conflicts — 已验证的硬件组合冲突案例
--    component_b_id 可空: 覆盖 "单组件 × 机型固件" 类冲突。
-- ============================================================
CREATE TABLE IF NOT EXISTS known_conflicts (
    id                  INTEGER PRIMARY KEY,
    model_id            INTEGER NOT NULL REFERENCES models(id),
    component_a_id      INTEGER NOT NULL REFERENCES components(id),
    component_b_id      INTEGER REFERENCES components(id),
    severity            TEXT NOT NULL CHECK (severity IN
                          ('no_boot','instability','performance_degradation',
                           'feature_loss','cosmetic')),
    description         TEXT NOT NULL,              -- 冲突现象与复现条件
    workaround          TEXT,                       -- 已知规避方法
    affected_macos_versions TEXT,                   -- 冲突出现的系统版本范围
    source_url          TEXT NOT NULL,
    corroboration_count INTEGER NOT NULL DEFAULT 1,
    extra_source_urls   TEXT,                       -- JSON 数组, 第 2..n 个印证来源
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- 索引
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_compat_model      ON compatibility (model_id, confidence_level);
CREATE INDEX IF NOT EXISTS idx_compat_component  ON compatibility (component_id);
CREATE INDEX IF NOT EXISTS idx_conflict_model    ON known_conflicts (model_id);
CREATE INDEX IF NOT EXISTS idx_models_identifier ON models (model_identifier);
