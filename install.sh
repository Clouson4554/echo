#!/bin/bash
set -e

echo "🚀 Echo v1.4 安装中..."

# 安装依赖
pip3 install -q flask paho-mqtt requests

# 创建数据目录
mkdir -p ~/.echo

# 安装主程序
cp echo_agent.py ~/.echo/
cp echo ~/.echo/
chmod +x ~/.echo/echo_agent.py ~/.echo/echo

# 安装 CLI（可选）
if [ -d ~/.local/bin ]; then
    cp ~/.echo/echo ~/.local/bin/echo
    chmod +x ~/.local/bin/echo
    echo "✅ CLI 已安装到 ~/.local/bin/echo"
else
    echo "ℹ️ 将 ~/.echo 添加到 PATH 以使用 ./echo 命令"
fi

echo ""
echo "✅ 安装完成！"
echo ""
echo "启动方式："
echo "  python3 ~/.echo/echo_agent.py"
echo ""
echo "或查看帮助："
echo "  python3 ~/.echo/echo --help"
echo ""
echo "查看路标："
echo "  python3 ~/.echo/echo welcome"
