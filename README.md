# x86 Mac 硬件升级顾问

查询 Intel Mac 型号支持的硬件升级方案，包括 Apple 官方规格与社区验证过的超规格方案。
所有数据按可信度分层存储，官方支持与论坛验证永不混层。

## 数据来源原则

仅从以下公开来源重新整理**事实性硬件参数**（不复制任何来源的页面文本或排版）：

| 来源 | 用途 | 允许的可信度层 |
|---|---|---|
| Apple support.apple.com 规格页 | 官方规格 | official |
| Wikipedia Mac 型号年表（CC 协议） | 官方规格交叉核对 | official |
| EveryMac.com（仅提取事实参数） | 官方规格交叉核对 | official |
| MacRumors 论坛 / Reddit / Dortania 文档等 | 社区验证方案 | community_tested / experimental |

可信度规则（由数据库触发器强制执行，见 `schema.sql`）：

- `official` 条目的来源类型必须是 apple_support / wikipedia / everymac，论坛来源插不进去；
- `community_tested` 要求 ≥2 个独立来源互相印证（`corroboration_count >= 2`），单一孤例只能标 `experimental`；
- 所有兼容性条目 `source_url` 必填，官方规格（models 表）也带 `apple_spec_url`，全部可溯源。

## 目录结构

```
mac-upgrade-advisor/
├── schema.sql                     # 数据库 schema（4 张表 + 分层约束触发器）
├── scripts/
│   ├── init_db.py                 # 建库 + 约束自检（零依赖，标准库 sqlite3）
│   ├── collect_official.py        # 批次1：抓取 Apple 规格页校验后入库 20 款机型
│   ├── collect_community.py       # 批次2：社区方案试点（3 款机型，来源 URL 逐一探测）
│   ├── collect_platform.py        # 批次3：平台层（控制器上限/端口/约束/GPU 驱动区间）
│   ├── audit_official.py          # 逐字段审计：Apple 页面原文 vs 库内数值并排比对
│   ├── lookup.py                  # 按机型查询（命令行展示层）
│   ├── advisor.py                 # 推荐引擎核心（第三阶段，CLI/Web 共用）
│   ├── advise.py                  # 推荐引擎 CLI
│   └── serve.py                   # 图形化界面（本地 Web UI，零依赖，仅监听 127.0.0.1）
├── 启动升级顾问.command            # Finder 里双击即可启动图形界面
└── data/
    ├── seed/
    │   ├── official_models.json   # 官方规格种子（含 Apple 规格页 URL + 页面校验标记）
    │   └── community_pilot.json   # 社区方案种子（每条 ≥2 个独立来源）
    └── mac_upgrade.db             # 生成的 SQLite 数据库（不入库）
```

## 使用

```bash
python3 scripts/init_db.py                    # 创建 data/mac_upgrade.db
python3 scripts/init_db.py --check            # 建库并在内存副本上自检约束/触发器
python3 scripts/collect_official.py           # 抓取 Apple 规格页逐条校验后入库（--dry-run 只校验）
python3 scripts/collect_community.py --verify # 探测全部社区来源 URL 后入库试点数据

python3 scripts/lookup.py --list              # 列出库内全部机型
python3 scripts/lookup.py MacBookPro11,4      # 按机型标识查
python3 scripts/lookup.py "mac mini 2012"     # 按名称模糊查

python3 scripts/serve.py                      # 图形界面: 启动后自动打开浏览器
                                              # (或在 Finder 双击 启动升级顾问.command)

python3 scripts/advise.py iMac12,2 --usage 秀肌肉 --risk experimental   # 推荐引擎 CLI
# 用途 (按升级哲学分档, 可简写):
#   轻度日用 — SSD/适量内存, 不碰非常规方案
#   黑苹果续命 — OCLP 跑新系统, 显卡按 Metal 支持过滤 (非 Metal 卡是死路)
#   Windows双系统 — 第二块盘优先; Windows 下显卡不受 macOS 驱动区间限制
#   秀肌肉 — 全部拉满不考虑实用性 (含内存/CPU 拉满推导、TB3 官方 eGPU)
#   野路子 — 彩蛋性质, 仅特殊机器解锁; 是秀肌肉的严格超集 (= 秀肌肉全部 + 非常规
#            实证如 iMac 换 E3 + TB1/2 eGPU 脚本), 独有条目带「野路子独有」标记;
#            无独有内容的机器不显示该选项
# 风险: official (仅官方) / community (默认) / experimental (含理论推导)
# 注: eGPU 是外接扩展而非机器本身升级, 只出现在 秀肌肉/野路子
```

采集脚本对每条数据打印来源 URL，供人工抽查校验；校验失败的条目不入库。
`audit_official.py` 会重新抓取每个 Apple 规格页，把处理器/内存/存储的页面原文与库内数值
并排输出（`python3 scripts/audit_official.py [机型标识]`），用于逐字段核对。

### 数据字段的来源边界

Apple 规格页**不包含**以下字段，它们来自其他允许的来源，准确性以对应来源为准：
- 具体 CPU 型号编号（如 i5-3470S）：Apple 只写主频/核数，型号编号来自 EveryMac/Wikipedia 交叉核对
- `cpu_socket`、`nvme_bootable`：社区拆解/实测知识（Dortania、iFixit 等）
- `max_macos`：来自 Apple 各版 macOS 的机型支持列表（不在规格页上）
- `board_id`：暂缺，待从 Dortania 机型表补齐

## 数据模型

- **platforms** — CPU 平台/内存控制器层：内存实际上限由控制器决定，同平台共享一条数据
  （来源 Intel ARK）。内存于是有三重上限：官方（Apple）/ 控制器理论（ARK）/ 社区实测（证据层）。
- **expansion_ports** — 机型的总线/接口清单（PCIe 槽、雷电版本、SATA 位、MXM、专有刀片槽等）。
  物理匹配 ≠ 可用，还要过 hw_constraints 与 gpu_arch_support（驱动）两道闸。
- **hw_constraints** — 固件/系统级约束（BootROM CPUID/微码检查、eGPU 支持政策、睡眠缺陷等），
  作用域分 model / platform / global，同三层可信度、可溯源。
- **gpu_arch_support** — GPU 架构 × macOS 驱动支持区间（Kepler、Polaris、RDNA 等），
  按架构世代统一管理，MXM / PCIe / eGPU 场景共用；数据源为 Dortania 等 hackintosh 文档。
- **cpu_options** — 机型的每档 CPU 配置（标配/选配、主频、核数、具体型号编号），
  一档一条记录；`models.cpu_model` 只保留基础款摘要。
- **models** — Mac 型号：机型标识（如 `MacBookPro11,4`）、逻辑板 ID、CPU、官方内存上限、
  原厂存储接口、NVMe 引导支持（`native` / `firmware_update_required` / `opencore_required` / `no`）。
- **components** — 硬件组件：内存 / SSD / 网卡等；支持"泛型规格条目"
  （如"任意 DDR3L-1600 SO-DIMM"）与具体产品条目共存。
- **compatibility** — 型号 × 组件兼容关系。同一组合允许多条记录（不同来源、不同可信度），
  "官方支持 16GB"与"论坛实测 32GB 可用"是两条独立记录。
- **known_conflicts** — 已验证的硬件组合冲突案例（只记录有来源的已验证案例，不穷举组合）。

## 路线图

- [x] 第一阶段：schema 设计与约束落地
- [x] 第二阶段：分批数据采集
  - [x] 批次1：26 款机型官方规格（Apple 规格页逐条抓取校验，页面标记匹配才入库）
  - [x] 批次2：社区方案试点后扩量至 10+ 机型（NVMe 家族、Mac Pro 双雄 CPU/内存超规格、
        iMac 超官方内存、MXM/CPU 换装），每条 ≥2 独立来源互相印证
  - [x] 批次3：平台层（20 平台控制器上限 / 69 条端口 / 固件约束 / GPU 架构驱动区间）
  - [ ] 待补：models.board_id（拟从 Dortania 机型表补齐，属社区来源）
- [x] 第三阶段：推荐引擎（输入：型号 / 用途 / 风险接受度；预算维度经评审移除；用途按升级哲学分五档，野路子方案单列不混入正常推荐）
  - 瓶颈诊断 → 用途权重排序 → 风险硬过滤 → 冲突/约束自动附着到对应推荐
  - 理论推导项（总线/控制器/驱动区间推导）仅运行时计算，标注"无实证"，排在实验层之下
  - 排序权重为编辑规则；事实性字段均可溯源
