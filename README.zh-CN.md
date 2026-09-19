# Typora 主题管理器

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**[English](README.md) | [简体中文](README.zh-CN.md)**

本地化的零依赖 [Typora](https://typora.io/) 主题安装器：管理任意 GitHub 上的
Typora 主题仓库，统一流水线：

**下载最新 release → 简易安全检查 → 基准清单比对 → 选择性安装 → 报告**

全部功能仅用 Python 标准库在本地完成——无需 `pip install`、无框架、无内置二进制。

### 特性

- **任意 GitHub 主题仓库** — 粘贴地址即可注册，安装结构（主题 CSS 位置、文件名
  模式、字体资源目录）自动推断。
- **安全优先**
  - 压缩包内脚本逐行规则表审查（`BLOCK` / `WARN` / `INFO` 三级），任一 `BLOCK`
    在解压前中止安装。
  - 路径穿越（zip-slip）与 CRC 完整性检查。
  - `.bat` / `.cmd` **不执行也不做内容分析**（仅统计数量）。
  - 压缩包文件清单与本地**基准清单**比对——结构意外变化（主题改名/增删文件）
    即取消安装；接受新清单永远需要人工显式确认。
- **选择性安装** — 只写入主题 CSS（→ 主题目录根）、资源目录与可选桥接文件；
  README、安装脚本、演示页等一律不进主题目录，多仓库共存互不污染。
- **精确卸载 / 清空重装** — 安装记录（`installed.json`）驱动移除范围；任何改动
  前先备份到主题目录同级的 `themes-backup-<id>-<时间戳>/`。
- **双语界面** — 简体中文（`zh-CN`）与英文（`en-US`），网页控制台与命令行
  输出/报告均支持；页头一键切换，或使用 `--lang`。
- **限流友好** — GitHub API 采用 ETag 条件请求；被限流时自动降级为重定向解析
  版本号 + 资产名探测。

### 环境要求

- Python **3.8+**（仅标准库）
- Windows / macOS / Linux

### 快速开始

**Windows** — 双击 `start-manager.bat`
（或在终端运行 `python theme_manager.py`）。

**macOS** — 双击 `start-manager.command`
（首次若被 Gatekeeper 拦截：右键 → 打开；或在终端运行 `bash start-manager.command`）。

**Linux** — 运行 `bash start-manager.command`
（或添加可执行位后在文件管理器中"在终端中运行"）。

网页控制台启动于 `http://127.0.0.1:17888/`（仅绑定本机回环地址）并自动打开浏览器。
粘贴一个 GitHub 主题仓库地址——例如
`https://github.com/sumruler/typora-theme-phycat`——点击**添加仓库**即可。

### 命令行

```bash
python theme_manager.py                        # 启动网页控制台（默认行为）
python theme_manager.py list                   # 查看仓库与安装状态（只读，安全）
python theme_manager.py check [ID ...]         # 检查最新版本（多仓库并行，只查询不安装）
python theme_manager.py install ID             # 安装/更新（会真实写入主题目录！）
python theme_manager.py install ID --mode clean       # 清空重装（仅本仓库文件，先备份）
python theme_manager.py install ID --update-manifest  # 人工核对差异后重建基准清单
python theme_manager.py uninstall ID           # 卸载（先备份）
python theme_manager.py add <URL>              # 添加仓库（本地已有安装则直接恢复）
python theme_manager.py rescan [ID ...]        # 刷新版本缓存+识别既有安装（只登记）
python theme_manager.py remove ID [--purge]    # 移除注册（--purge 同时删本地数据）
python theme_manager.py --lang en-US list      # 以英文输出（也可设 TTM_LANG 环境变量）
```

退出码：`0` 成功 · `1` 下载/查询失败 · `2` 安全检查不通过 · `3` 安装失败 ·
`4` 清单不一致。

### 文件去向

| 路径 | 用途 |
|---|---|
| `repos.json` | 仓库注册表（缺失时自动以默认值创建） |
| `repos/<id>/zips/<tag>/` | 下载的 release 归档（保留最新 N 个，见 `settings.keep_zips`） |
| `repos/<id>/manifest.json` | 防投毒用的基准文件清单 |
| `repos/<id>/installed.json` | 驱动卸载/清空重装的安装记录 |
| `repos/<id>/latest.json` | 版本检查缓存（含 ETag 与降级探测记忆） |
| `repos/<id>/安装报告.md` | 最近一次安装报告（每仓库一份，每次运行重写） |

真实的 Typora 主题目录按平台自动探测（Windows `%APPDATA%\Typora\themes`、
macOS `~/Library/Application Support/abnerworks.Typora/themes`、
Linux `~/.config/Typora/themes`）。备份创建在主题目录同级，且永远不会被自动清理。

### 许可证

以 [MIT 许可证](LICENSE) 开源发布。
