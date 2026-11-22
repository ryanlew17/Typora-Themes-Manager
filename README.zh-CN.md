# Typora 主题管理器

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**[English](README.md) | [简体中文](README.zh-CN.md)**

本地化的零依赖 [Typora](https://typora.io/) 主题安装器，管理任意 GitHub 主题仓库：
**获取安装包 → 安全检查 → 基准清单比对 → 选择性安装 → 报告**。
仅用 Python 标准库实现。

安装包按以下顺序自动降级获取，没有压缩包 release 的仓库也能使用：

1. **release 压缩包** — 最新 release 的 zip / tar 系资产（7z/rar 标准库无法
   解包，提示后跳过）；
2. **release 附件散文件** — 作者直接挂载在 release 上的 css / 字体文件；
3. **源码树** — 仓库默认分支，适用于无可用 release 资产的仓库，只下载与
   安装相关的文件；
4. **手动匹配** — 由你选择本地主题文件（控制台文件选择器或 `match` 命令），
   按内容哈希与全部 release（新→旧）及源码树对齐，来自任一历史版本的文件
   都能命中。

手动匹配成功的仓库基于本地记录的结构管理：更新只覆盖你未自定义的文件，
缺失文件自动恢复，卸载与清空重装严格只动记录在案的文件；检查更新只比对
记录的哈希与仓库侧内容，不校验本地文件，你的自定义不会被误报为待更新。
上游新增的文件不会自动纳入，需要时重新执行一次手动匹配即可登记。

### 快速开始

需要 Python **3.8+**（Windows / macOS / Linux）。

- **Windows** — 双击 `start-manager.bat`
- **macOS** — 双击 `start-manager.command`（若被 Gatekeeper 拦截：右键 → 打开）
- **Linux** — 运行 `bash start-manager.command`

网页控制台启动于 `http://127.0.0.1:17888/` 并自动打开浏览器。粘贴一个主题
仓库地址——例如 `https://github.com/sumruler/typora-theme-phycat`——点击
**添加仓库**即可。控制台与命令行均为双语（English / 简体中文）。

### 命令行

```bash
python theme_manager.py                 # 启动网页控制台（默认行为）
python theme_manager.py list            # 查看仓库与安装状态（只读）
python theme_manager.py check [ID ...]  # 检查最新版本（不安装）
python theme_manager.py install ID      # 安装 / 更新
python theme_manager.py uninstall ID    # 卸载（先备份）
python theme_manager.py add <URL>       # 添加仓库
python theme_manager.py match ID FILE... # 手动匹配本地文件（按哈希登记）
python theme_manager.py remove ID       # 移除仓库
```

其余选项（`--mode clean`、`--update-manifest`、`--allow-remote-host`、
`--lang` 等）见 `--help`。退出码：`0` 成功 · `1` 下载/查询失败 ·
`2` 安全检查不通过 · `3` 安装失败 · `4` 清单不一致 ·
`5` 暂停：等待远程主机确认（未做任何改动）。

### 安全性

- 安装包内脚本在解压前逐行审查，发现可疑行为立即中止安装；`.bat` / `.cmd`
  一律不执行。
- 主题 CSS 若引用远程主机，安装会暂停，待你核对并同意主机清单后才继续
  （使用这类主题会向这些主机暴露你的 IP 与使用时间）。
- 安装包文件清单与本地基准比对，结构意外变化即取消安装，需人工确认后才会
  接受。
- 只写入主题 CSS 与资源文件；任何移除操作都先备份，且绝不波及其他仓库的
  主题。
- 手动匹配只在本仓库数据目录内登记（结构、哈希、时间戳），绝不改动主题
  目录。

### 许可证

以 [MIT 许可证](LICENSE) 开源发布。
