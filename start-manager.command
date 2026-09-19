#!/usr/bin/env bash
# 启动 Typora 主题管理器网页控制台（自动打开浏览器）
# macOS: 双击本文件（首次若被拦截，右键 → 打开）；也可在终端执行 bash start-manager.command
# 注意：本文件名必须保持纯 ASCII——macOS 双击启动 .command 时 Finder→Terminal 传递
# 非 ASCII 文件名会被错误转码，导致 Terminal 执行乱码路径而报「文件不存在」。
# Linux: 右键 → 在终端中运行（或先 chmod +x 后直接执行）

cd "$(dirname "$0")" || exit 1

# 固定 Python 输出编码为 UTF-8，防止 shell 环境变量异常时中文变乱码
export PYTHONIOENCODING=utf-8:replace

# 依次探测 python3 / python，均要求 Python 3.8+
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 \
       && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
        PY="$c"
        break
    fi
done

if [ -z "$PY" ]; then
    echo
    echo "[错误] 未检测到可用的 Python 3.8+ 环境。"
    echo "请先从 Python 官网下载安装: https://www.python.org/downloads/"
    echo
    echo "即将为您打开下载页面 ..."
    if command -v open >/dev/null 2>&1; then
        open "https://www.python.org/downloads/"        # macOS
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "https://www.python.org/downloads/"    # Linux
    fi
    printf "按回车键退出 ..."
    read -r _
    exit 1
fi

"$PY" "theme_manager.py" "$@"

echo
echo "管理器已停止。"
printf "按回车键关闭窗口 ..."
read -r _
exit 0
