#!/bin/zsh
# 双击启动 x86 Mac 硬件升级顾问 (本地 Web 界面)
cd "$(dirname "$0")"

# 检查数据库是否存在，不存在则自动初始化
if [ ! -f "data/mac_upgrade.db" ]; then
    echo "数据库不存在，正在自动初始化..."
    python3 scripts/init_db.py
    if [ $? -ne 0 ]; then
        echo "数据库初始化失败，请检查错误信息"
        read -p "按回车键退出..."
        exit 1
    fi
    echo "数据库初始化完成"
fi

exec python3 scripts/serve.py
