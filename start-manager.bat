@echo off
rem 双击运行本脚本即可启动 Typora 主题管理器网页控制台（自动打开浏览器）
rem 注意: 本文件必须以 ANSI/GBK 编码保存（中文 Windows 默认），请勿另存为 UTF-8

title Typora Themes Manager
cd /d "%~dp0"

rem 依次探测 py 启动器与 python，均要求 Python 3.8+（低于此版本或仅存
rem 微软商店占位程序时探测失败，与未安装同样处理）
set "PYCMD="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PYCMD=python"
)

if not defined PYCMD (
    echo.
    echo [错误] 未检测到可用的 Python 3.8+ 环境。
    echo 请先从 Python 官网下载安装，安装时记得勾选 "Add python.exe to PATH":
    echo.
    echo     https://www.python.org/downloads/
    echo.
    echo 即将为您打开下载页面 ...
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)

%PYCMD% "theme_manager.py" %*

echo.
echo 管理器已停止。
pause
