# Typora 主题管理器

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**[English](README.md) | [简体中文](README.zh-CN.md)**

本地化的零依赖 [Typora](https://typora.io/) 主题安装器，管理任意 GitHub 主题仓库：
**下载最新 release → 安全检查 → 基准清单比对 → 选择性安装 → 报告**。
仅用 Python 标准库实现。

### 快速开始

需要 Python **3.8+**（Windows / macOS / Linux）。

- **Windows** — 双击 `start-manager.bat`
- **macOS** — 双击 `start-manager.command`（若被 Gatekeeper 拦截：右键 → 打开）
- **Linux** — 运行 `bash start-manager.command`

网页控制台启动于 `http://127.0.0.1:17888/` 并自动打开浏览器。
粘贴一个主题仓库地址——例如 `https://github.com/sumruler/typora-theme-phycat`——
点击**添加仓库**即可。控制台与命令行均为双语（English / 简体中文）。

### 命令行

```bash
python theme_manager.py                 # 启动网页控制台（默认行为）
python theme_manager.py list            # 查看仓库与安装状态（只读）
python theme_manager.py check [ID ...]  # 检查最新版本（不安装）
python theme_manager.py install ID      # 安装 / 更新
python theme_manager.py uninstall ID    # 卸载（先备份）
python theme_manager.py add <URL>       # 添加仓库
python theme_manager.py remove ID       # 移除仓库
```

其余选项（`--mode clean`、`--update-manifest`、`--allow-remote-host`、
`--lang` 等）见 `--help`。退出码：`0` 成功 · `1` 下载/查询失败 ·
`2` 安全检查不通过 · `3` 安装失败 · `4` 清单不一致 ·
`5` 暂停：等待远程主机确认（未做任何改动）。

### 安全性

- 压缩包内脚本在解压前逐行审查，发现可疑行为立即中止安装；
  `.bat` / `.cmd` 一律不执行。
- 主题 CSS 若引用远程主机，安装会暂停，待你核对并同意主机清单后才继续
  （使用这类主题会向这些主机暴露你的 IP 与使用时间）。
- 压缩包文件清单与本地基准比对，结构意外变化即取消安装，需人工确认后才会接受。
- 只写入主题 CSS 与资源文件；任何移除操作都先备份，且绝不波及其他仓库的主题。

### 许可证

以 [MIT 许可证](LICENSE) 开源发布。
