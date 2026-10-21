#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Typora 主题仓库管理器（仅 Python 标准库；Windows / macOS / Linux 通用）

本工具由「下载 → 安全审查 → 解压安装」通用流水线构成，可管理任意 GitHub 上的
Typora 主题仓库（在 repos.json 中注册即可，添加新仓库时自动推断安装结构）:

  1. 获取最新 release，按版本归档下载到 repos/<id>/zips/<tag>/（保留最新 N 个，默认 2）
  2. 简易安全检查（zip-slip / CRC / 脚本逐行规则审查；bat/cmd 按约定不做内容分析，
     因为本工具不执行它们）
  3. 与本地基准清单（仅文件路径，不含大小/时间）比对；结构不一致（主题改名/增删文件）
     时直接取消安装，报告差异，由用户显式确认后才能以新清单继续
  4. 选择性安装：仅把主题 CSS 写入 Typora 主题目录根、资源目录(字体等)就位，
     可选生成默认主题桥接；README/安装脚本等其余文件不写入主题目录，
     多个仓库的主题可共存互不污染
  5. installed.json 记录本仓库写入的每个文件 → 支持卸载、以及只清理本仓库文件的
     「清空重装」（清空前先备份到主题目录同级 themes-backup-<id>-<时间戳>/）
  6. 每仓库固定一份 install-report.md（失败路径同样写报告）

本地网页控制台（零框架零依赖，仅标准库）:
  python theme_manager.py            # 启动本地网页控制台(127.0.0.1，自动打开浏览器)
命令行(无界面模式):
  python theme_manager.py list                                   # 列出仓库与状态
  python theme_manager.py check [ID ...]                         # 检查最新版本(多仓库并行)
  python theme_manager.py install ID [--mode merge|clean] \
        [--update-manifest] [--keep-zips N]                      # 安装/更新
  python theme_manager.py uninstall ID                           # 卸载(先备份)
  python theme_manager.py add <仓库URL或owner/repo> [--name X]   # 添加新仓库(先检索本地遗留
        # 数据与主题目录，主题已装则直接恢复为已安装状态)
  python theme_manager.py rescan [ID ...]                        # 重新索引：刷新版本缓存并识别
        # 主题目录中的既有安装(只登记，不改动主题目录)
  python theme_manager.py remove ID [--purge]                    # 移除仓库(可选清空数据)
  python theme_manager.py themes-dir                             # 显示主题目录

退出码: 0=成功  1=下载/查询失败  2=安全检查不通过  3=安装失败  4=文件清单不一致
        5=暂停等待CSS远程主机确认(未做任何改动)
"""

import argparse
import concurrent.futures
import contextlib
import fnmatch
import gzip
import http.server
import json
import os
import platform
import posixpath
import re
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "repos.json"
DATA_DIR = SCRIPT_DIR / "repos"
UI_FILE = SCRIPT_DIR / "ui.html"
REPORT_NAME = "install-report.md"
VERSION = "0.1.1"
USER_AGENT = "Typora-Themes-Manager/%s (local; Python-urllib)" % VERSION
DEFAULT_PORT = 17888

# 网络超时(秒)：收紧失败反馈；下载大文件单独放宽
API_TIMEOUT = 12
REDIRECT_TIMEOUT = 15
HEAD_TIMEOUT = 10
DOWNLOAD_TIMEOUT = 120

EXIT_OK = 0
EXIT_DOWNLOAD = 1
EXIT_SECURITY = 2
EXIT_INSTALL = 3
EXIT_MANIFEST = 4
# CSS 远程主机待确认：安装已在任何写入之前暂停。与 2（安全检查不通过）分开，
# 让脚本化调用能区分「内容有恶意」与「等待用户确认外联」
EXIT_CONFIRM = 5

SEV_ORDER = {"BLOCK": 0, "WARN": 1, "INFO": 2}

# 需要逐行深扫的脚本类扩展名（.bat/.cmd 除外——本工具不执行它们，按约定不做内容分析）
SCRIPT_EXTS = {".ps1", ".sh", ".vbs", ".js", ".jse", ".wsf", ".hta", ".py"}
SKIPPED_EXTS = {".bat", ".cmd"}
PE_EXTS = {".exe", ".dll", ".scr", ".pif", ".com", ".msi", ".jar", ".lnk"}
FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2"}
# 位于主题目录根但不算主题的 css（Typora 自定义机制文件）
ROOT_DOC_CSS = {"github.user.css", "base.user.css"}

# (正则, 等级, 消息键) —— 逐行匹配脚本内容；BLOCK 任何一条命中即拦截安装。
# 第三列是 MESSAGES 的键，展示时经 t() 翻译
SCRIPT_RULES = [
    # 联网 / 上传类
    (r"\b(curl|wget)\b", "BLOCK", "sec_rule_curl"),
    (r"\bpowershell\b|\bpwsh\b", "BLOCK", "sec_rule_powershell"),
    (r"\bmshta\b|\bbitsadmin\b|\bcertutil\b", "BLOCK", "sec_rule_sys_tools"),
    (r"Invoke-WebRequest|Invoke-RestMethod|Net\.WebClient|DownloadFile|UploadFile|Start-BitsTransfer",
     "BLOCK", "sec_rule_net_cmd"),
    (r"\b(ftp|scp|sftp|ssh|netcat)\b", "BLOCK", "sec_rule_remote"),
    (r"https?://", "BLOCK", "sec_rule_url"),
    (r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "BLOCK", "sec_rule_ip"),
    # 混淆 / 编码载荷
    (r"-EncodedCommand|FromBase64String|\b-certutil\b|-decode\b|base64\s+(-d|--decode)\b",
     "BLOCK", "sec_rule_base64"),
    # 持久化 / 账户 / 系统配置
    (r"reg\s+add\b.*?(Run|RunOnce)\b", "BLOCK", "sec_rule_autorun"),
    (r"schtasks\b.*(/create|/Create)|\bsc\s+create\b|New-ScheduledTask|New-Service\b",
     "BLOCK", "sec_rule_persist"),
    (r"\bnet\s+(user|localgroup)\b", "BLOCK", "sec_rule_accounts"),
    (r"\bnetsh\b", "BLOCK", "sec_rule_netsh"),
    (r"reg\s+save\b|\bprocdump\b|\bmimikatz\b|\blsass\b", "BLOCK", "sec_rule_creds"),
    (r"\b(format|diskpart|vssadmin|bcdedit|chkdsk)\b", "BLOCK", "sec_rule_disk"),
    (r"\bshutdown\b", "BLOCK", "sec_rule_shutdown"),
    # 全盘枚举 / 信息收集类（记录但不拦截）
    (r"\bfor\s+/r\b|\bdir\b[^&|]*\s/s\b|\btree\b\s+/f|\bwmic\b|Get-ChildItem\b.*-Recurse|Get-PSDrive|LogicalDisk",
     "WARN", "sec_rule_enum"),
    (r"\b(netstat|ipconfig|whoami|tasklist|systeminfo|arp)\b", "WARN", "sec_rule_sysinfo"),
    (r"\breg\s+(add|delete|query|import)\b", "WARN", "sec_rule_registry"),
    (r"\bosascript\b", "WARN", "sec_rule_osascript"),
]

# 删除类指令：目标限定在主题目录内视为安装器预期行为，否则拦截
DELETE_RE = re.compile(r"^\s*(?:@\w+\s+)?(?:.*\s)?\b(del|erase|rd|rmdir|rm)\b", re.IGNORECASE)
SAFE_SCOPE_HINTS_BASE = ("theme_dir", "%appdata%", "themes", "typora", "%~dp0", "script_dir")

CSS_URL_KW_RE = re.compile(r"url\(|@import", re.IGNORECASE)
CSS_REMOTE_RE = re.compile(r"^(?:https?:)?//", re.IGNORECASE)
CSS_ESCAPE_RE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})(?:\r\n|[ \t\r\n\f])?|(\r\n|[\r\n\f])|(.))",
                           re.DOTALL)


# ------------------------------------------------------------ 多语言 ----

LANGS = ("zh-CN", "en-US")
# 公开仓库默认语言为英文；中文系统经 detect_system_lang 自动检测仍得到中文界面
DEFAULT_LANG = "en-US"
# 当前语言：t() 读取。CLI 由 --lang/TTM_LANG 初始化，UI 由 /api/lang 切换
# （写入只发生在启动与用户显式切换时，任务线程只读，无并发写风险）
CURRENT_LANG = DEFAULT_LANG


def t(key, **kw):
    """按当前语言取消息模板并填充占位符；缺键回退默认语言，仍缺则原样返回键名。"""
    tbl = MESSAGES.get(CURRENT_LANG) or {}
    text = tbl.get(key) or MESSAGES[DEFAULT_LANG].get(key) or key
    return text.format(**kw) if kw else text


def set_lang(lang, persist=False):
    """归一化并设置当前语言；persist=True 时写入 repos.json settings.lang。"""
    global CURRENT_LANG
    lang = (lang or "").strip()
    if lang.startswith("en"):
        lang = "en-US"
    elif lang.startswith("zh"):
        lang = "zh-CN"
    if lang not in LANGS:
        return CURRENT_LANG
    CURRENT_LANG = lang
    if persist:
        with CONFIG_LOCK:
            cfg = load_config()
            cfg["settings"]["lang"] = lang
            save_config(cfg)
    return lang


def detect_system_lang():
    """探测系统界面语言（仅作首次默认值，失败/未识别回退默认语言 en-US）。"""
    try:
        if sys.platform == "win32":
            import ctypes
            primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0xFF
            if primary == 0x09:  # English
                return "en-US"
            if primary == 0x04:  # Chinese
                return "zh-CN"
        else:
            for var in ("LC_ALL", "LANG"):
                v = os.environ.get(var, "")
                if v.startswith("en"):
                    return "en-US"
                if v.startswith("zh"):
                    return "zh-CN"
    except Exception:
        pass
    return DEFAULT_LANG


def init_lang(cli_lang=None):
    """语言初始化（优先级: CLI 参数/环境变量 > settings.lang > 系统检测 > 默认）。"""
    lang = cli_lang or os.environ.get("TTM_LANG") or ""
    if not lang:
        try:
            lang = load_config()["settings"].get("lang") or ""
        except Exception:
            lang = ""
    return set_lang(lang or detect_system_lang())


# 消息字典：所有用户可见文本（CLI/日志/报告/HTTP 应答）的唯一来源，
# 键为稳定标识，值为带 {占位符} 的模板；新增语言在此补一列即可
MESSAGES = {
    "zh-CN": {
        # 通用
        "none": "无",
        "unknown": "未知",
        "root_dir_label": "(根目录)",
        "root_dir_log": "(root)",
        # 配置
        "err_config_corrupt": "仓库注册表 {name} 已损坏({exc})，请人工检查",
        "err_repo_not_found": "未注册的仓库 id: {id}（现有: {repos}）",
        # 下载与网络
        "api_304": "  GitHub API 304：内容未变（未消耗 API 配额）",
        "api_unavailable": "  GitHub API 不可用({err})，改用 releases/latest 重定向解析 ...",
        "err_redirect_parse": "无法从 releases/latest 重定向({url})解析最新版本号",
        "err_no_asset_name": "GitHub API 限流，且无法推断 release 资产名，请稍后重试",
        "err_no_zip_asset": "最新 release 没有可用的 zip 资产",
        "err_multi_zip": "release 含多个 zip 资产({assets})，请在 repos.json 中为仓库 {id} 配置 asset_re",
        "err_asset_name": "无效的下载文件名: {name}",
        "dl_skip": "  本地已存在同版本文件，跳过下载: {name}",
        "dl_progress": "\r  下载中 {mb:.1f} MB",
        "dl_done": "\r  下载完成: {name} ({mb:.2f} MB)",
        "err_download_incomplete": "下载不完整({got} / {want} 字节)，请重试",
        "bad_zip": "下载的文件不是有效的 zip 压缩包，已删除，请重试",
        # 基准清单
        "err_manifest_corrupt": "本地基准清单 {name} 已损坏({exc})，请人工检查，或核对当前压缩包后"
                                "用 install --update-manifest 重建",
        # 安全检查发现项说明
        "sec_crc_fail": "CRC 校验失败，压缩包损坏或被篡改",
        "sec_zip_slip": "路径穿越条目(zip-slip)，拒绝解压",
        "sec_delete_in_scope": "删除/清空操作目标位于主题目录内(安装器预期行为)",
        "sec_delete_out_scope": "删除操作目标在主题目录之外，拒绝执行",
        "sec_pe_binary": "压缩包内含可执行二进制({ext})，请自行确认用途",
        "sec_rule_curl": "出现命令行下载/上传工具(curl/wget)",
        "sec_rule_powershell": "调用 PowerShell",
        "sec_rule_sys_tools": "出现常被滥用的系统工具(mshta/bitsadmin/certutil)",
        "sec_rule_net_cmd": "出现联网下载/上传指令",
        "sec_rule_remote": "出现远程传输/反弹类工具",
        "sec_rule_url": "脚本内出现网络链接",
        "sec_rule_ip": "脚本内出现裸 IP 地址",
        "sec_rule_base64": "出现 Base64 编码/解码的混淆载荷",
        "sec_rule_autorun": "写注册表自启动项(Run/RunOnce)",
        "sec_rule_persist": "创建计划任务/系统服务(持久化)",
        "sec_rule_accounts": "创建/修改系统账户",
        "sec_rule_netsh": "修改网络/防火墙配置",
        "sec_rule_creds": "凭据转储相关指令",
        "sec_rule_disk": "磁盘级危险操作",
        "sec_rule_shutdown": "关机指令",
        "sec_rule_enum": "递归/全盘枚举类操作",
        "sec_rule_sysinfo": "系统信息收集类命令",
        "sec_rule_registry": "注册表操作",
        "sec_rule_osascript": "调用 AppleScript(macOS)",
        # 主题目录与结构推断
        "err_no_appdata": "未找到 APPDATA 环境变量，无法定位 Typora 主题目录",
        "err_no_css": "压缩包中未找到任何 CSS 文件，无法推断主题结构",
        "bridge_comment": "/* {name} 默认主题桥接(自动生成；删除本文件即可恢复 Typora 原默认样式) */",
        # 报告骨架
        "report_title": "# {name} 安装报告",
        "report_title_sub": "# {name} 安装报告 — {sub}",
        "report_runtime": "- 运行时间: {time}",
        "report_platform": "- 平台: {platform} (Python {py})",
        "report_repo": "- 仓库: {url}",
        "report_conclusion": "结论",
        "report_error_section": "错误",
        "report_saved": "报告已写入: {path}",
        "report_save_failed": "报告写入失败: {exc}",
        "findings_header": "| 文件 | 等级 | 说明 | 位置 |",
        "findings_line_no": "行 {lines}",
        # 安装流程
        "sub_merge": "合并保留现有主题",
        "sub_clean": "清空重装(仅本仓库, 先备份)",
        "sub_uninstall": "卸载",
        "step_release": "[1/6] 查询 GitHub 最新 release ...",
        "log_latest": "  最新版本: {tag} — {name}",
        "sec1_title": "1. Release 信息",
        "sec1_version": "- 版本: **{tag}** — {name}",
        "sec1_published": "- 发布时间: {time}",
        "published_unknown": "未知(API 限流，降级解析时省略)",
        "sec1_source": "- 元数据来源: {source}",
        "source_api_304": "GitHub API(ETag 304，内容未变且未消耗 API 配额)",
        "source_api": "GitHub API",
        "source_redirect": "releases/latest 重定向解析(GitHub API 限流)",
        "sec1_asset": "- 资产: `{name}`{size}",
        "size_mb": " ({mb:.2f} MB)",
        "size_unknown": " (大小未知)",
        "sec1_url": "- 下载地址: {url}",
        "sec2_title": "2. 下载与旧包清理",
        "dl_state_new": "- 状态: 新下载",
        "dl_state_skip": "- 状态: 本地已存在同版本同大小文件，跳过下载",
        "sec2_archive": "- 压缩包归档: `repos/{id}/zips/{tag}/`（按版本保留最新 {keep} 个）",
        "sec2_pruned": "- 已删除旧版本归档: {names}",
        "step_scan": "[2/6] 安全检查 {name} ...",
        "sec3_title": "3. 安全检查",
        "sec3_summary": "- 压缩包共 {files} 个条目；CSS {css} 个，字体 {fonts} 个，脚本类文件 {scripts} 个{script_names}，"
                       "bat/cmd {bat} 个（按约定跳过内容分析，本工具不执行它们）",
        "sec3_integrity_ok": "- 完整性(CRC)与路径穿越(zip-slip)检查: 通过",
        "sec3_integrity_bad": "- 完整性(CRC)与路径穿越(zip-slip)检查: 发现问题，见下表",
        "sec3_remote": "- CSS 远程资源主机: {hosts}（使用主题时可能向这些主机发起请求，暴露 IP 地址与使用时间；安装前需确认）",
        "remote_confirm_msg": "安装已暂停，主题目录未改动。请确认是否允许主题使用远程资源: {hosts}",
        "remote_confirm_cli": "请先核对主机清单；同意后重新安装，为每个主机重复添加 --allow-remote-host HOST。",
        "err_remote_hosts": "approved_remote_hosts 必须是主机名字符串列表",
        "sec3_clean": "已扫描的脚本未命中审查规则；CSS 远程主机及未扫描的 bat/cmd 文件见上方统计。",
        "sec3_fail_line": "**安全检查结论: ❌ 不通过（BLOCK {block} 条，WARN {warn} 条），已中止安装，未解压、未执行任何脚本。**",
        "sec3_fail_log": "  安全检查不通过: BLOCK {n} 条, 已中止",
        "sec3_fail_msg": "安全检查未通过(BLOCK {n} 条)，本次未做任何安装改动",
        "sec3_fail_finish": "❌ {msg}。请人工审查上表中的 BLOCK 项后再决定是否继续。",
        "sec3_pass_line": "**安全检查结论: ✅ 通过（无 BLOCK；WARN {warn} 条、INFO 若干条已列明）**",
        "sec3_pass_log": "  安全检查通过 (WARN {n} 条)",
        "step_infer": "[3/6] 首次安装，推断压缩包结构 ...",
        "step_manifest": "[3/6] 比对本地基准清单 ...",
        "sec4_title": "4. 结构推断",
        "sec4_inferred": "- 新仓库首次安装，已按压缩包内容自动推断并保存安装配置: 主题 CSS 位于 `{css_dir}`，"
                        "主题文件名模式 `{theme_re}`，资源目录 {assets}",
        "infer_log": "  推断结果: css_dir={css_dir} theme_re={theme_re} assets={assets}",
        "sec5_title": "5. 基准清单比对",
        "sec5_manifest": "- 基准清单: `{name}`（仅文件路径，不含大小/时间等元数据）",
        "sec5_rebuilt": "- 已按 --update-manifest 以当前压缩包重建基准清单（版本 {tag}，{n} 个文件）",
        "sec5_created": "- 本地无基准清单，已依据本次通过安全检查的压缩包建立基准（版本 {tag}，{n} 个文件）",
        "sec5_created_log": "  已建立基准清单: {n} 个文件",
        "sec5_mismatch": "- ❌ 与基准（版本 {ver}，{n} 个文件）不一致：",
        "sec5_added": "  - 新增 {n} 个: {files}",
        "sec5_removed": "  - 消失 {n} 个: {files}",
        "sec5_mismatch_log": "  清单不一致: 新增 {n} 个, 消失 {m} 个, 已取消安装",
        "mismatch_msg": "文件清单与基准不一致，已取消安装（未解压、未改动主题目录）",
        "mismatch_finish": "❌ 文件清单与基准不一致，已取消安装（未解压、未改动主题目录）。"
                           "请人工核对上方差异（主题改名/增删文件等）；确认新版变更合理后，"
                           "运行 `python theme_manager.py install {id} --update-manifest` 重建基准清单并完成安装。",
        "sec5_match": "- ✅ 与基准（版本 {ver}，{n} 个文件）完全一致",
        "sec5_match_log": "  清单一致: {n} 个文件",
        "step_install": "[4/6] 安装到 Typora 主题目录 ...",
        "themes_dir_log": "  Typora 主题目录: {dir}",
        "sec6_title": "6. 解压与安装",
        "sec6_dir": "- Typora 主题目录: `{dir}`",
        "no_theme_css_msg": "压缩包中没有匹配 {re} 的主题 CSS，结构与配置不符",
        "install_fail_finish": "❌ 安装失败：{msg}。",
        "clean_log": "  清空重装: 先备份并移除本仓库旧文件 ...",
        "clean_done": "- 清空重装: 仅清理本仓库文件（不影响其他主题），已备份 {n} 个文件至 `{dir}` 并移除",
        "clean_done_log": "  已移除本仓库旧文件 {n} 个(备份于 {dir})",
        "clean_nothing": "- 清空重装: 本仓库此前无已安装文件，直接安装",
        "sec6_installed": "- 选择性安装 {n} 个主题 CSS（压缩包内 {css_dir}，已置于主题目录根）与资源目录 {assets}（{m} 个文件）",
        "sec6_skipped": "- 已跳过压缩包内其余 {n} 个文件（README/安装脚本/演示页等，不写入主题目录）",
        "bridge_done": "- 默认主题桥接: 已生成 `{file}` → {target}{saved}",
        "bridge_saved_suffix": "（旧文件已备份）",
        "bridge_missing": "- 默认主题桥接: 未生成（缺少桥接目标主题 {theme}）",
        "bridge_generated": "已生成",
        "bridge_not_generated": "未生成",
        "step_verify": "[5/6] 验证安装结果 ...",
        "sec7_title": "7. 安装验证",
        "sec7_count": "- 主题目录根匹配 {re} 的 CSS 数量: **{n}**（以 {min}+ 为通过线）",
        "sec7_themes": "- 已安装主题: {names}",
        "sec7_bridge": "- `{file}` 桥接: {state}（删除该文件即可恢复 Typora 原默认外观）",
        "sec7_backups": "- 本仓库备份目录（位于主题目录同级，最近 3 个）: {names}",
        "step_record": "[6/6] 写入安装记录 ...",
        "install_ok_msg": "安装成功：{tag}（{n} 个主题已就位）。重启 Typora 后在「主题」菜单中选择 {name} 主题即可。",
        "install_ok_log": "  安装成功: {n} 个主题",
        "install_insufficient": "安装完成但主题数量不足（仅 {n} 个，低于 {min}），请检查安装报告与主题目录。",
        "run_aborted": "运行中断：{exc}",
        "err_log": "错误: {exc}",
        # 卸载
        "uninstall_detail": "卸载明细",
        "uninstall_dir": "- Typora 主题目录: `{dir}`",
        "uninstall_nothing_msg": "本仓库没有可卸载的文件（尚未安装）",
        "uninstall_nothing_line": "- {msg}",
        "uninstall_nothing_finish": "ℹ️ {msg}，无需卸载。",
        "uninstall_backup_log": "  备份并移除 {n} 个文件 ...",
        "uninstall_removed": "- 已备份 {n} 个文件至 `{dir}` 并从主题目录移除",
        "uninstall_more": "  - ... 等共 {n} 个",
        "uninstall_ok_msg": "已卸载 {name}（{n} 个文件，备份于 {dir}）。重启 Typora 生效。",
        "uninstall_ok_log": "  卸载完成: {n} 个文件",
        "uninstall_fail_msg": "卸载失败：{exc}",
        "uninstall_fail_finish": "❌ 卸载失败：{exc}",
        # 版本检查
        "state_uptodate": "✓ 已是最新",
        "state_newer": "有新版本 {tag}（当前 {cur}）",
        "state_local_newer": "本地 {cur} 较新（最新 {tag}）",
        "state_not_installed": "尚未安装",
        "check_state_log": "  {id}: 当前 {cur}, 最新 {tag} — {state}",
        "check_latest_log": "  {id}: 最新 {tag}（本仓库尚未安装）",
        "check_ok_msg": "已检查 {id}：最新 {tag}，{state}",
        "check_fail_msg": "检查失败：{exc}",
        "check_fail_log": "  {id}: {msg}",
        # 并行任务
        "parallel_none": "没有已注册的仓库",
        "parallel_running": "{verb} {slug} ...",
        "verb_check": "检查",
        "verb_rescan": "重新索引",
        "parallel_summary": "已并行{verb} {n} 个仓库{fail}{adopt}",
        "parallel_fail_part": "，{n} 个失败",
        "parallel_all_ok": "，全部成功",
        "parallel_adopt_part": "，识别既有安装 {n} 个",
        # 本地安装识别
        "src_manifest": "基准清单 manifest.json",
        "src_zip_archive": "本地归档 zips/{tag}",
        "src_installed": "旧安装记录 installed.json",
        "src_release_asset": "release 资产清单",
        "src_local_manifest": "本地基准清单",
        "infer_local_fail": "  本地结构推断失败(来自{src}): {exc}",
        "infer_local_restored": "  已从{src}恢复结构: css_dir={css_dir} theme_re={theme_re} assets_dirs={assets_dirs}",
        "version_compare_missing": "  与{label}({ver})比对: 既有安装缺少 {n} 个文件({files}{more})",
        "more_suffix": " 等",
        "no_themes_dir": "未探测到 Typora 主题目录，无法识别既有安装",
        "no_matching_css": "主题目录根没有匹配 {re} 的 CSS，本仓库视为未安装",
        "adopt_manifest_created": "  本地无基准清单，已按 {tag} 资产建立（{n} 个文件）",
        "adopt_ok_msg": "识别成功：主题目录检出 {n} 个主题 CSS、共 {m} 个文件，已登记为已安装（{ver}）",
        "ver_unknown": "版本未知",
        "rescan_step1": "  [1/3] 刷新最新版本缓存 ...",
        "rescan_check_exc": "  版本检查异常: {exc}",
        "rescan_check_failed": "  版本查询未成功（可能离线或被限流），继续本地识别 ...",
        "rescan_step2": "  [2/3] 确定安装结构 ...",
        "rescan_no_structure": "缺少结构信息，且无法获取 release 资产（版本查询失败且本地无缓存），暂不能识别；联网后重试",
        "rescan_download_infer": "  本地无结构线索，下载 release 资产推断结构: {asset}",
        "rescan_inferred_log": "  已按 release 资产推断结构: theme_re={re}",
        "rescan_step3": "  [3/3] 扫描 Typora 主题目录，识别既有安装 ...",
        "rescan_ok_with_check": "{msg}；版本缓存已刷新",
        "rescan_fail_msg": "重新索引失败：{exc}",
        # 仓库增删
        "err_parse_url": "无法解析仓库地址: {url}（支持 https://github.com/owner/repo 或 owner/repo）",
        "err_already_registered": "仓库 {repo} 已注册（id: {id}）",
        "add_legacy_found": "  检测到本地遗留数据 repos/{id}/（此前移除时未清空），尝试恢复 ...",
        "add_added": "  已添加仓库 {slug}（id: {id}）",
        "add_no_clue": "  本地无结构线索；将在联网「重新索引」或首次安装时推断结构",
        "remove_purged": "  已移除仓库 {slug} 并清空其数据目录 repos/{id}/（主题文件未动，重新添加后"
                         "可用 rescan 识别回已安装）",
        "remove_kept": "  已移除仓库 {slug}（数据保留在 repos/{id}/，主题文件未动；重新添加时会自动恢复）",
        # 任务与网页服务
        "internal_error": "内部错误: {exc}",
        "err_bad_host": "非法 Host",
        "err_no_header": "缺少 X-Local-Tool 头",
        "err_not_found": "not found",
        "err_no_report": "尚无报告",
        "err_busy": "有任务正在进行中，请稍候",
        "err_bad_mode": "mode 必须是 merge 或 clean",
        "err_lang_invalid": "lang 必须是 {langs} 之一",
        "err_ui_missing_title": "ui.html 缺失",
        "err_ui_missing_body": "请将 ui.html 放在 theme_manager.py 同目录。",
        "ui_started": "Typora 主题管理器已启动: {url} （Ctrl+C 退出）",
        "ui_exited": "已退出。",
        "err_ports_busy": "端口 {lo}~{hi} 均被占用",
        # CLI
        "cli_description": "Typora 主题仓库管理器（多仓库下载/安全检查/安装，仅标准库）",
        "help_lang": "界面/报告/输出语言（默认: settings.lang > 系统语言 > en-US）",
        "help_ui": "启动本地网页控制台（不带子命令时的默认行为）",
        "help_no_open": "不自动打开浏览器",
        "help_list": "列出已注册仓库与状态",
        "help_check": "检查仓库最新版本（默认全部）",
        "help_rescan": "重新索引：刷新版本缓存并识别主题目录中的既有安装（只登记，不改动主题目录）",
        "help_install": "安装/更新指定仓库的主题",
        "help_mode": "merge=合并保留现有主题(默认), clean=清空重装(仅本仓库文件, 先备份)",
        "help_update_manifest": "以当前压缩包重建基准清单(人工核对差异报告后使用)",
        "help_allow_remote_host": "允许主题使用指定的 CSS 远程主机（核对清单后使用，可重复指定）",
        "help_keep_zips": "保留的最新版本压缩包数量(默认取 repos.json 中的 settings.keep_zips)",
        "help_uninstall": "卸载指定仓库的主题(先备份)",
        "help_add": "添加新仓库(URL 或 owner/repo)",
        "help_name": "显示名称(默认用仓库名)",
        "help_remove": "从注册表移除仓库",
        "help_purge": "同时删除 repos/<id>/ 数据目录",
        "help_themes_dir": "显示 Typora 主题目录路径",
        "cli_themes_dir": "Typora 主题目录: {dir}",
        "cli_themes_dir_none": "(未探测到)",
        "cli_registered": "已注册 {n} 个仓库:",
        "cli_empty_hint": "  （空）用 `python {prog} add <仓库URL>` 添加第一个仓库",
        "cli_state_installed": "已安装 {ver}（检出 {n} 个主题）",
        "cli_state_adopted": "已识别安装·版本未知（检出 {n} 个主题）",
        "cli_state_unregistered": "未登记（主题目录检出 {n} 个主题文件，运行 rescan 可识别）",
        "cli_state_not_installed": "未安装",
        "cli_latest_suffix": " | 最新 {tag}",
        "cli_uptodate_suffix": " ✓最新",
        "cli_check_failed_suffix": " | 检查失败: {err}",
    },
    "en-US": {
        # common
        "none": "none",
        "unknown": "unknown",
        "root_dir_label": "(root)",
        "root_dir_log": "(root)",
        # config
        "err_config_corrupt": "Repository registry {name} is corrupted ({exc}); please inspect it manually",
        "err_repo_not_found": "Unknown repo id: {id} (available: {repos})",
        # download & network
        "api_304": "  GitHub API 304: content unchanged (no API quota consumed)",
        "api_unavailable": "  GitHub API unavailable ({err}); falling back to releases/latest redirect ...",
        "err_redirect_parse": "Cannot parse latest tag from releases/latest redirect ({url})",
        "err_no_asset_name": "GitHub API is rate-limited and the release asset name cannot be inferred; try again later",
        "err_no_zip_asset": "The latest release has no usable zip asset",
        "err_multi_zip": "Release contains multiple zip assets ({assets}); please set asset_re for repo {id} in repos.json",
        "err_asset_name": "Invalid download filename: {name}",
        "dl_skip":  "  Same-version file already exists locally, skipping download: {name}",
        "dl_progress": "\r  Downloading {mb:.1f} MB",
        "dl_done": "\r  Download complete: {name} ({mb:.2f} MB)",
        "err_download_incomplete": "Download incomplete ({got} / {want} bytes); please retry",
        "bad_zip": "The downloaded file is not a valid zip archive; it has been deleted, please retry",
        # baseline manifest
        "err_manifest_corrupt": "Local baseline manifest {name} is corrupted ({exc}); please inspect it manually, "
                                "or verify the current zip and rebuild it with install --update-manifest",
        # security findings
        "sec_crc_fail": "CRC check failed; archive is corrupted or tampered with",
        "sec_zip_slip": "Path traversal entry (zip-slip); refusing to extract",
        "sec_delete_in_scope": "Delete/clear operation targets inside the themes directory (expected installer behavior)",
        "sec_delete_out_scope": "Delete operation targets outside the themes directory; refusing",
        "sec_pe_binary": "Archive contains an executable binary ({ext}); please verify its purpose yourself",
        "sec_rule_curl": "Command-line download/upload tool present (curl/wget)",
        "sec_rule_powershell": "Invokes PowerShell",
        "sec_rule_sys_tools": "Commonly abused system tools present (mshta/bitsadmin/certutil)",
        "sec_rule_net_cmd": "Network download/upload commands present",
        "sec_rule_remote": "Remote transfer / reverse-shell style tool present",
        "sec_rule_url": "Network URL inside script",
        "sec_rule_ip": "Bare IP address inside script",
        "sec_rule_base64": "Base64-encoded/decoded obfuscated payload present",
        "sec_rule_autorun": "Writes registry autostart entries (Run/RunOnce)",
        "sec_rule_persist": "Creates scheduled tasks / system services (persistence)",
        "sec_rule_accounts": "Creates/modifies system accounts",
        "sec_rule_netsh": "Modifies network/firewall configuration",
        "sec_rule_creds": "Credential-dumping related commands",
        "sec_rule_disk": "Disk-level destructive operation",
        "sec_rule_shutdown": "Shutdown command",
        "sec_rule_enum": "Recursive / whole-drive enumeration",
        "sec_rule_sysinfo": "System information gathering commands",
        "sec_rule_registry": "Registry operations",
        "sec_rule_osascript": "Invokes AppleScript (macOS)",
        # themes dir & structure inference
        "err_no_appdata": "APPDATA environment variable not found; cannot locate the Typora themes directory",
        "err_no_css": "No CSS file found in the archive; cannot infer the theme structure",
        "bridge_comment": "/* {name} default theme bridge (auto-generated; delete this file to restore Typora's original default look) */",
        # report skeleton
        "report_title": "# {name} Install Report",
        "report_title_sub": "# {name} Install Report — {sub}",
        "report_runtime": "- Run time: {time}",
        "report_platform": "- Platform: {platform} (Python {py})",
        "report_repo": "- Repository: {url}",
        "report_conclusion": "Conclusion",
        "report_error_section": "Error",
        "report_saved": "Report written: {path}",
        "report_save_failed": "Failed to write report: {exc}",
        "findings_header": "| File | Level | Description | Location |",
        "findings_line_no": "line {lines}",
        # install pipeline
        "sub_merge": "merge, keeping existing themes",
        "sub_clean": "clean reinstall (this repo only, backs up first)",
        "sub_uninstall": "uninstall",
        "step_release": "[1/6] Querying latest GitHub release ...",
        "log_latest": "  Latest version: {tag} — {name}",
        "sec1_title": "1. Release info",
        "sec1_version": "- Version: **{tag}** — {name}",
        "sec1_published": "- Published: {time}",
        "published_unknown": "unknown (omitted when API is rate-limited and we fall back to redirect parsing)",
        "sec1_source": "- Metadata source: {source}",
        "source_api_304": "GitHub API (ETag 304, content unchanged, no API quota consumed)",
        "source_api": "GitHub API",
        "source_redirect": "releases/latest redirect parsing (GitHub API rate-limited)",
        "sec1_asset": "- Asset: `{name}`{size}",
        "size_mb": " ({mb:.2f} MB)",
        "size_unknown": " (size unknown)",
        "sec1_url": "- Download URL: {url}",
        "sec2_title": "2. Download & old archive cleanup",
        "dl_state_new": "- Status: newly downloaded",
        "dl_state_skip": "- Status: same-version file of same size already exists locally; download skipped",
        "sec2_archive": "- Zip archive: `repos/{id}/zips/{tag}/` (keeps the latest {keep} versions)",
        "sec2_pruned": "- Removed old version archives: {names}",
        "step_scan": "[2/6] Security scan {name} ...",
        "sec3_title": "3. Security scan",
        "sec3_summary": "- Archive contains {files} entries; {css} CSS, {fonts} fonts, {scripts} script files{script_names}, "
                       "{bat} bat/cmd (content analysis skipped by convention; this tool never executes them)",
        "sec3_integrity_ok": "- Integrity (CRC) and path traversal (zip-slip) checks: passed",
        "sec3_integrity_bad": "- Integrity (CRC) and path traversal (zip-slip) checks: problems found, see table below",
        "sec3_remote": "- CSS remote resource hosts: {hosts} (using this theme may contact these hosts and expose "
                       "your IP address and usage times; confirmation required before installation)",
        "remote_confirm_msg": "Installation paused; the themes directory is unchanged. Confirm whether to allow "
                              "remote theme resources from: {hosts}",
        "remote_confirm_cli": "Review the host list first; to consent, retry with --allow-remote-host HOST "
                              "repeated for each host.",
        "err_remote_hosts": "approved_remote_hosts must be a list of hostname strings",
        "sec3_clean": "No review rules matched the scanned scripts; see the CSS hosts and unscanned bat/cmd counts above.",
        "sec3_fail_line": "**Security scan verdict: ❌ FAILED (BLOCK {block}, WARN {warn}); installation aborted — "
                          "nothing was extracted and no script was executed.**",
        "sec3_fail_log": "  Security scan failed: {n} BLOCK findings, aborted",
        "sec3_fail_msg": "Security scan failed ({n} BLOCK findings); no changes were made",
        "sec3_fail_finish": "❌ {msg}. Please review the BLOCK items in the table above before deciding whether to continue.",
        "sec3_pass_line": "**Security scan verdict: ✅ PASSED (no BLOCK; {warn} WARN and some INFO items listed above)**",
        "sec3_pass_log": "  Security scan passed ({n} WARN)",
        "step_infer": "[3/6] First install; inferring archive structure ...",
        "step_manifest": "[3/6] Comparing against baseline manifest ...",
        "sec4_title": "4. Structure inference",
        "sec4_inferred": "- First install of this repo: installation config inferred from the archive and saved: theme CSS "
                        "in `{css_dir}`, theme file pattern `{theme_re}`, asset dirs {assets}",
        "infer_log": "  Inferred: css_dir={css_dir} theme_re={theme_re} assets={assets}",
        "sec5_title": "5. Baseline manifest comparison",
        "sec5_manifest": "- Baseline manifest: `{name}` (file paths only; no size/time metadata)",
        "sec5_rebuilt": "- Baseline manifest rebuilt from the current archive via --update-manifest (version {tag}, {n} files)",
        "sec5_created": "- No local baseline manifest; baseline established from this archive that passed the security scan "
                        "(version {tag}, {n} files)",
        "sec5_created_log": "  Baseline manifest established: {n} files",
        "sec5_mismatch": "- ❌ Differs from baseline (version {ver}, {n} files):",
        "sec5_added": "  - {n} added: {files}",
        "sec5_removed": "  - {n} removed: {files}",
        "sec5_mismatch_log": "  Manifest mismatch: {n} added, {m} removed; installation cancelled",
        "mismatch_msg": "File list differs from the baseline; installation cancelled (nothing extracted, themes directory untouched)",
        "mismatch_finish": "❌ File list differs from the baseline; installation cancelled (nothing extracted, themes "
                           "directory untouched). Please review the differences above (theme renames, added/removed files, "
                           "etc.); once the new version's changes look reasonable, run "
                           "`python theme_manager.py install {id} --update-manifest` to rebuild the baseline and finish the install.",
        "sec5_match": "- ✅ Exactly matches the baseline (version {ver}, {n} files)",
        "sec5_match_log": "  Manifest matches: {n} files",
        "step_install": "[4/6] Installing into the Typora themes directory ...",
        "themes_dir_log": "  Typora themes directory: {dir}",
        "sec6_title": "6. Extract & install",
        "sec6_dir": "- Typora themes directory: `{dir}`",
        "no_theme_css_msg": "No theme CSS in the archive matches {re}; structure does not match the config",
        "install_fail_finish": "❌ Installation failed: {msg}.",
        "clean_log": "  Clean reinstall: backing up and removing this repo's old files first ...",
        "clean_done": "- Clean reinstall: only this repo's files were cleaned (other themes untouched); "
                      "{n} files backed up to `{dir}` and removed",
        "clean_done_log": "  Removed {n} old files of this repo (backed up in {dir})",
        "clean_nothing": "- Clean reinstall: no previously installed files for this repo; installing directly",
        "sec6_installed": "- Selectively installed {n} theme CSS files ({css_dir} in the archive, placed at the themes "
                          "directory root) and asset dirs {assets} ({m} files)",
        "sec6_skipped": "- Skipped the other {n} files in the archive (README/install scripts/demo pages etc.; "
                        "not written into the themes directory)",
        "bridge_done": "- Default theme bridge: generated `{file}` → {target}{saved}",
        "bridge_saved_suffix": " (old file backed up)",
        "bridge_missing": "- Default theme bridge: not generated (bridge target theme {theme} is missing)",
        "bridge_generated": "generated",
        "bridge_not_generated": "not generated",
        "step_verify": "[5/6] Verifying installation ...",
        "sec7_title": "7. Install verification",
        "sec7_count": "- CSS files at the themes directory root matching {re}: **{n}** (pass line: {min}+)",
        "sec7_themes": "- Installed themes: {names}",
        "sec7_bridge": "- `{file}` bridge: {state} (delete that file to restore Typora's original default look)",
        "sec7_backups": "- Backup dirs for this repo (next to the themes directory; latest 3): {names}",
        "step_record": "[6/6] Writing install record ...",
        "install_ok_msg": "Installed successfully: {tag} ({n} themes in place). Restart Typora and pick a {name} theme "
                          "from the Themes menu.",
        "install_ok_log": "  Installed successfully: {n} themes",
        "install_insufficient": "Installation finished but the theme count is below the threshold ({n} < {min}); "
                                "please check the install report and the themes directory.",
        "run_aborted": "Run aborted: {exc}",
        "err_log": "Error: {exc}",
        # uninstall
        "uninstall_detail": "Uninstall details",
        "uninstall_dir": "- Typora themes directory: `{dir}`",
        "uninstall_nothing_msg": "This repo has nothing to uninstall (not installed yet)",
        "uninstall_nothing_line": "- {msg}",
        "uninstall_nothing_finish": "ℹ️ {msg}; nothing to uninstall.",
        "uninstall_backup_log": "  Backing up and removing {n} files ...",
        "uninstall_removed": "- Backed up {n} files to `{dir}` and removed them from the themes directory",
        "uninstall_more": "  - ... {n} in total",
        "uninstall_ok_msg": "Uninstalled {name} ({n} files, backed up in {dir}). Restart Typora to take effect.",
        "uninstall_ok_log": "  Uninstall complete: {n} files",
        "uninstall_fail_msg": "Uninstall failed: {exc}",
        "uninstall_fail_finish": "❌ Uninstall failed: {exc}",
        # version check
        "state_uptodate": "✓ up to date",
        "state_newer": "new version {tag} available (current {cur})",
        "state_local_newer": "local {cur} is newer (latest {tag})",
        "state_not_installed": "not installed yet",
        "check_state_log": "  {id}: current {cur}, latest {tag} — {state}",
        "check_latest_log": "  {id}: latest {tag} (this repo is not installed)",
        "check_ok_msg": "Checked {id}: latest {tag}, {state}",
        "check_fail_msg": "Check failed: {exc}",
        "check_fail_log": "  {id}: {msg}",
        # parallel jobs
        "parallel_none": "No registered repositories",
        "parallel_running": "{verb} {slug} ...",
        "verb_check": "Checking",
        "verb_rescan": "Rescanning",
        "parallel_summary": "{verb} finished for {n} repos{fail}{adopt}",
        "parallel_fail_part": ", {n} failed",
        "parallel_all_ok": ", all succeeded",
        "parallel_adopt_part": ", adopted {n} existing installs",
        # local install adoption
        "src_manifest": "baseline manifest manifest.json",
        "src_zip_archive": "local archive zips/{tag}",
        "src_installed": "old install record installed.json",
        "src_release_asset": "release asset listing",
        "src_local_manifest": "local baseline manifest",
        "infer_local_fail": "  Local structure inference failed (from {src}): {exc}",
        "infer_local_restored": "  Structure restored from {src}: css_dir={css_dir} theme_re={theme_re} assets_dirs={assets_dirs}",
        "version_compare_missing": "  Compared against {label} ({ver}): existing install is missing {n} files ({files}{more})",
        "more_suffix": " etc.",
        "no_themes_dir": "Typora themes directory not found; cannot adopt an existing install",
        "no_matching_css": "No CSS at the themes directory root matches {re}; this repo counts as not installed",
        "adopt_manifest_created": "  No local baseline manifest; established from the {tag} asset ({n} files)",
        "adopt_ok_msg": "Adopted successfully: {n} theme CSS and {m} files in total detected in the themes directory; "
                        "registered as installed ({ver})",
        "ver_unknown": "version unknown",
        "rescan_step1": "  [1/3] Refreshing latest-version cache ...",
        "rescan_check_exc": "  Version check error: {exc}",
        "rescan_check_failed": "  Version query did not succeed (possibly offline or rate-limited); continuing local adoption ...",
        "rescan_step2": "  [2/3] Determining install structure ...",
        "rescan_no_structure": "Structure info is missing and the release asset cannot be fetched (version query failed "
                               "and no local cache); cannot adopt for now — retry when online",
        "rescan_download_infer": "  No local structure clues; downloading the release asset to infer the structure: {asset}",
        "rescan_inferred_log": "  Structure inferred from the release asset: theme_re={re}",
        "rescan_step3": "  [3/3] Scanning the Typora themes directory to adopt existing installs ...",
        "rescan_ok_with_check": "{msg}; version cache refreshed",
        "rescan_fail_msg": "Rescan failed: {exc}",
        # repo add/remove
        "err_parse_url": "Cannot parse repository address: {url} (supports https://github.com/owner/repo or owner/repo)",
        "err_already_registered": "Repository {repo} is already registered (id: {id})",
        "add_legacy_found": "  Found leftover local data repos/{id}/ (not purged on a previous removal); trying to restore ...",
        "add_added": "  Added repository {slug} (id: {id})",
        "add_no_clue": "  No local structure clues; the structure will be inferred on the next online 'rescan' "
                       "or the first install",
        "remove_purged": "  Removed repository {slug} and purged its data directory repos/{id}/ (theme files untouched; "
                         "re-add and rescan to re-adopt as installed)",
        "remove_kept": "  Removed repository {slug} (data kept in repos/{id}/, theme files untouched; "
                       "it will be restored automatically when re-added)",
        # jobs & web server
        "internal_error": "Internal error: {exc}",
        "err_bad_host": "Illegal Host header",
        "err_no_header": "Missing X-Local-Tool header",
        "err_not_found": "not found",
        "err_no_report": "No report yet",
        "err_busy": "A job is already running; please wait",
        "err_bad_mode": "mode must be merge or clean",
        "err_lang_invalid": "lang must be one of {langs}",
        "err_ui_missing_title": "ui.html missing",
        "err_ui_missing_body": "Please place ui.html next to theme_manager.py.",
        "ui_started": "Typora Themes Manager started: {url} (Ctrl+C to quit)",
        "ui_exited": "Exited.",
        "err_ports_busy": "Ports {lo}~{hi} are all in use",
        # CLI
        "cli_description": "Typora theme repository manager (multi-repo download / security scan / install; stdlib only)",
        "help_lang": "UI/report/output language (default: settings.lang > system language > en-US)",
        "help_ui": "Start the local web console (default when no subcommand is given)",
        "help_no_open": "Do not open the browser automatically",
        "help_list": "List registered repos and their status",
        "help_check": "Check the latest versions (all repos by default)",
        "help_rescan": "Rescan: refresh the version cache and adopt existing installs found in the themes directory "
                       "(registers only; never touches the themes directory)",
        "help_install": "Install/update themes from the given repo",
        "help_mode": "merge=keep existing themes (default), clean=clean reinstall (this repo's files only, backs up first)",
        "help_update_manifest": "Rebuild the baseline manifest from the current archive (use after manually reviewing "
                                "the diff report)",
        "help_allow_remote_host": "Allow a CSS remote host after reviewing the host list (repeat for each host)",
        "help_keep_zips": "Number of latest-version zips to keep (default: settings.keep_zips in repos.json)",
        "help_uninstall": "Uninstall the given repo's themes (backs up first)",
        "help_add": "Add a new repository (URL or owner/repo)",
        "help_name": "Display name (defaults to the repo name)",
        "help_remove": "Remove a repository from the registry",
        "help_purge": "Also delete the repos/<id>/ data directory",
        "help_themes_dir": "Print the Typora themes directory path",
        "cli_themes_dir": "Typora themes directory: {dir}",
        "cli_themes_dir_none": "(not detected)",
        "cli_registered": "{n} repositories registered:",
        "cli_empty_hint": "  (empty) add your first repo with `python {prog} add <REPO_URL>`",
        "cli_state_installed": "installed {ver} ({n} themes found)",
        "cli_state_adopted": "adopted install · version unknown ({n} themes found)",
        "cli_state_unregistered": "unregistered ({n} theme files found in the themes directory; run rescan to adopt)",
        "cli_state_not_installed": "not installed",
        "cli_latest_suffix": " | latest {tag}",
        "cli_uptodate_suffix": " ✓up to date",
        "cli_check_failed_suffix": " | check failed: {err}",
    },
}


# ------------------------------------------------------------ 配置与路径 ----

def load_config():
    if not CONFIG_PATH.exists():
        # clone/解压后的首次运行，只读命令（list/check/--help）也不落盘——
        # 保持「只读命令零写入」约定（smoke_test_release 有断言）；默认注册表
        # 由控制台启动（run_ui → ensure_default_config）或首次变更类操作写入。
        return {"settings": {"keep_zips": 2}, "repos": {}}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(t("err_config_corrupt", name=CONFIG_PATH.name, exc=exc))
    data.setdefault("settings", {"keep_zips": 2})
    data.setdefault("repos", {})
    repos = data["repos"]
    if isinstance(repos, list):  # 旧版列表格式 → 自动迁移为以 id 为键的字典并回写
        data["repos"] = {c["id"]: c for c in repos if isinstance(c, dict) and c.get("id")}
        save_config(data)
    return data


def save_config(cfg_all):
    _write_config_atomic(cfg_all)


def ensure_default_config():
    """首次运行（网页控制台启动）生成默认注册表；已存在则不动。
    原子写且不取 CONFIG_LOCK——冷启动时尚无并发写者，内容确定故无害。"""
    if CONFIG_PATH.exists():
        return
    _write_config_atomic({"settings": {"keep_zips": 2}, "repos": {}})


def _write_config_atomic(cfg_all):
    """先写临时文件再 os.replace，避免并发或中断在磁盘上留下半截 repos.json。"""
    tmp = CONFIG_PATH.with_name("%s.tmp-%d-%x" % (CONFIG_PATH.name, os.getpid(),
                                                  threading.get_ident()))
    try:
        tmp.write_text(json.dumps(cfg_all, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# repos.json 读改写锁：并行任务(检查全部/重新索引全部)与网页端增删仓库可能同时写注册表，
# 所有「load → 修改 → save」必须整体持锁（update_repo_cfg），否则会互相丢更新
CONFIG_LOCK = threading.Lock()


def update_repo_cfg(cfg):
    """线程安全地把单个仓库配置写回注册表（重新读取-合并-保存）。"""
    with CONFIG_LOCK:
        cfg_all = load_config()
        cfg_all["repos"][cfg["id"]] = cfg
        save_config(cfg_all)


def repo_dir(cfg):
    return DATA_DIR / cfg["id"]


def find_repo(cfg_all, repo_id):
    cfg = cfg_all["repos"].get(repo_id)
    if cfg is None:
        raise RuntimeError(t("err_repo_not_found",
                             id=repo_id, repos=", ".join(cfg_all["repos"]) or t("none")))
    return cfg


def slug(cfg):
    return "%s/%s" % (cfg["owner"], cfg["repo"])


def load_json_if_exists(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def load_latest_cache(cfg):
    """读取 repos/<id>/latest.json 检查缓存（缺失/损坏时返回空 dict）。"""
    return load_json_if_exists(repo_dir(cfg) / "latest.json") or {}


# ------------------------------------------------------------ 下载 release ----

def github_api_url(s):
    return "https://api.github.com/repos/%s/releases/latest" % s


def github_latest_page(s):
    return "https://github.com/%s/releases/latest" % s


def github_download_url(s, tag, name):
    return "https://github.com/%s/releases/download/%s/%s" % (s, tag, name)


def url_exists(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=HEAD_TIMEOUT) as resp:
            return resp.status == 200
    except Exception:
        return False


def resolve_latest_tag_by_redirect(s):
    """免 API 降级方案: releases/latest 会 302 到 /releases/tag/<tag>。"""
    req = urllib.request.Request(github_latest_page(s), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REDIRECT_TIMEOUT) as resp:
        final = resp.geturl()
    m = re.search(r"/releases/tag/([^/?#]+)$", final)
    if not m:
        raise RuntimeError(t("err_redirect_parse", url=final))
    return m.group(1)


def _api_get_json(url, etag=""):
    """GET JSON：gzip 压缩传输 + 可选 If-None-Match 条件请求。
    返回 (数据, 新 etag)；304 且携带 etag 时数据为 None（复用调用方缓存，
    且条件请求命中时不消耗 GitHub 匿名配额）。"""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
               "Accept-Encoding": "gzip"}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            new_etag = resp.headers.get("ETag") or ""
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            raw = resp.read()
    except urllib.error.HTTPError as err:
        if err.code == 304 and etag:  # urllib 会把 304 当错误抛出
            new_etag = err.headers.get("ETag") or etag
            err.close()
            return None, new_etag
        raise
    if enc == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8")), new_etag


def _release_from_cache(cache, s):
    """由 latest.json 缓存重建 release dict（304 复用路径）。"""
    tag = cache.get("tag") or "?"
    asset = cache.get("asset") or ""
    url = cache.get("asset_url") or (github_download_url(s, tag, asset) if asset else "")
    return {"tag_name": tag, "name": cache.get("name") or "",
            "published_at": cache.get("published_at") or "",
            "assets": [{"name": asset, "size": int(cache.get("asset_size") or 0),
                        "browser_download_url": url}]}


def probe_asset_name(cfg, tag):
    """降级路径：HEAD 逐一探测常见资产命名；优先上次命中的命名模式。
    返回 (资产名, 模式序号)，全部落空时 (None, None)。"""
    s = slug(cfg)
    pats = ("%s-%s.zip" % (cfg["repo"], tag),
            "%s_%s.zip" % (cfg["repo"], tag),
            "%s.zip" % cfg["repo"])
    order = list(range(len(pats)))
    last = load_latest_cache(cfg).get("asset_pattern")
    if isinstance(last, int) and 0 <= last < len(pats):
        order.sort(key=lambda i: i != last)  # 上次命中的模式排最前
    for i in order:
        if url_exists(github_download_url(s, tag, pats[i])):
            return pats[i], i
    return None, None


def fetch_latest_release(cfg, log=print, cache=None):
    """查询最新 release。优先 GitHub API（带 ETag 条件请求，304 时零配额消耗、
    直接复用 latest.json 缓存）；API 不可用/限流时降级为重定向解析 + 资产名探测。
    返回 meta dict: release / source('api'|'redirect') / etag /
    asset_pattern(降级探测命中的模式序号) / not_modified(304 命中)。"""
    s = slug(cfg)
    cache = cache or {}
    # 缓存含 tag 与资产名即可携带 etag（error 标记不影响：旧投影仍与 etag 对应一致）
    usable = bool(cache.get("tag") and cache.get("asset"))
    etag = (cache.get("etag") or "") if usable else ""
    try:
        data, new_etag = _api_get_json(github_api_url(s), etag=etag)
        if data is None:  # 304：远端与本地缓存一致
            log(t("api_304"))
            return {"release": _release_from_cache(cache, s), "source": "api",
                    "etag": new_etag, "asset_pattern": None, "not_modified": True}
        return {"release": data, "source": "api", "etag": new_etag,
                "asset_pattern": None, "not_modified": False}
    except (urllib.error.URLError, OSError, ValueError) as api_err:
        log(t("api_unavailable", err=api_err))
    tag = resolve_latest_tag_by_redirect(s)
    asset_name, pattern_idx = probe_asset_name(cfg, tag)
    if asset_name is None:
        raise RuntimeError(t("err_no_asset_name"))
    release = {
        "tag_name": tag, "name": "", "published_at": "",
        "assets": [{"name": asset_name, "size": 0,
                    "browser_download_url": github_download_url(s, tag, asset_name)}],
    }
    return {"release": release, "source": "redirect", "etag": "",
            "asset_pattern": pattern_idx, "not_modified": False}


def pick_asset(release, cfg):
    cands = [a for a in release.get("assets", [])
             if a.get("name", "").lower().endswith(".zip")
             and "source code" not in a.get("name", "").lower()]
    if not cands:
        raise RuntimeError(t("err_no_zip_asset"))
    asset_re = cfg.get("asset_re")
    if asset_re:
        prefer = [a for a in cands if re.match(asset_re, a["name"])]
        if prefer:
            return prefer[0]
    if len(cands) == 1:
        return cands[0]
    raise RuntimeError(t("err_multi_zip",
                         assets=", ".join(a["name"] for a in cands), id=cfg["id"]))


def _zip_readable(path):
    try:
        with zipfile.ZipFile(path) as zf:
            return bool(zf.namelist())
    except Exception:
        return False


def download_asset(asset, dest_dir, log=print):
    """返回 (文件路径, 是否新下载)；已存在同名且同大小(或本身完好)的文件则跳过。"""
    # 远端元数据不作为路径使用；同时处理两种分隔符，避免跨平台路径穿越。
    name = Path(asset["name"].replace("\\", "/")).name
    if name in ("", ".", "..") or ":" in name or "\x00" in name:
        raise RuntimeError(t("err_asset_name", name=asset["name"]))
    dest_dir.mkdir(parents=True, exist_ok=True)
    expected = int(asset.get("size") or 0)
    dest = dest_dir / name
    if dest.exists() and (not expected or dest.stat().st_size == expected) \
            and (expected or _zip_readable(dest)):
        log(t("dl_skip", name=name))
        return dest, False

    tmp = dest_dir / (name + ".part")
    req = urllib.request.Request(asset["browser_download_url"],
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, open(tmp, "wb") as f:
        total = 0
        last = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
            if total - last >= 512 * 1024:  # 每 512KB 汇报一次进度
                last = total
                log(t("dl_progress", mb=total / 1048576), end="")
    log(t("dl_done", name=name, mb=total / 1048576) + " " * 24)
    if expected and total != expected:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(t("err_download_incomplete", got=total, want=expected))
    tmp.replace(dest)
    return dest, True


def version_key(tag):
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)", tag or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)


def prune_zip_versions(rd, keep, log=print):
    """repos/<id>/zips/ 下按 tag 版本号保留最新 keep 个，返回 (保留, 删除) 列表。"""
    zips_root = rd / "zips"
    if not zips_root.is_dir():
        return [], []
    vers = sorted((p for p in zips_root.iterdir() if p.is_dir()),
                  key=lambda p: (version_key(p.name), p.name), reverse=True)
    kept, removed = vers[:keep], vers[keep:]
    for p in removed:
        shutil.rmtree(p, ignore_errors=True)
    return kept, removed


# ------------------------------------------------------------ 基准清单 ----

def zip_logical_files(zip_path):
    """返回压缩包的逻辑文件清单(剥离单一顶层目录后的相对路径，正斜杠，已排序)
    与顶层目录名(无剥离时为 None)。"""
    with zipfile.ZipFile(zip_path) as zf:
        norm = [zi.filename.replace("\\", "/") for zi in zf.infolist() if not zi.is_dir()]
    tops = {n.split("/")[0] for n in norm if "/" in n}
    strip = bool(tops) and len(tops) == 1 and all("/" in n for n in norm)
    files = sorted(n.split("/", 1)[1] if strip else n for n in norm)
    return files, (next(iter(tops)) if strip else None)


def load_manifest(rd):
    path = rd / "manifest.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("files", [])), data.get("version", t("unknown"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(t("err_manifest_corrupt", name=path.name, exc=exc))


def save_manifest(rd, files, version):
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "manifest.json").write_text(json.dumps({
        "version": version,
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": sorted(files),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ------------------------------------------------------------ 安全检查 ----

def read_zip_text(zf, name):
    raw = zf.read(name)
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _css_tokens(text):
    """把 CSS 词法切分为 (kind, value) 序列，kind ∈ code/string；注释折算为
    一个空格并入 code。只切分不解析语法：字符串边界遵循 CSS 转义规则
    （\\" 不闭合），从而把 content:"url(https://…)" 这类字符串内文本与
    真实引用区分开。"""
    tokens, buf = [], []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            buf.append(" ")
            continue
        if ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1  # 转义字符不闭合字符串
            tokens.append(("code", "".join(buf)))
            buf = []
            tokens.append(("string", text[i + 1:j]))
            i = j + 1
            continue
        buf.append(ch)
        i += 1
    tokens.append(("code", "".join(buf)))
    return tokens


def _url_operand(rest, tokens, idx):
    """提取 url( 的操作数：裸值取到右括号；引号值要求 url( 与引号之间仅空白，
    操作数即紧随的字符串字面量内容。"""
    if not rest.strip():
        nxt = tokens[idx + 1] if idx + 1 < len(tokens) else None
        return nxt[1] if nxt and nxt[0] == "string" else ""
    return rest.split(")", 1)[0]


def normalize_host(host):
    """主机名归一化：去空白与尾部点、转小写、IDNA 转 ASCII（punycode），使扫描
    值与 --allow-remote-host 手输值（unicode/punycode 两种写法）可互相匹配；
    含非法字符（下划线等）或超长标签编码失败时保留原样——两侧同用本函数仍精确匹配。"""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return ""
    try:
        return host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return host


def _remote_host_of(operand):
    """操作数是 http(s)/协议相对 URL 时返回归一化主机名，否则 None。"""
    operand = operand.strip()
    if not CSS_REMOTE_RE.match(operand):
        return None
    try:
        host = urllib.parse.urlsplit(
            "https:" + operand if operand.startswith("//") else operand).hostname
    except ValueError:
        return None
    return normalize_host(host) if host else None


def css_remote_hosts(text):
    """静态列出 CSS url()/@import 直接引用的 HTTP(S) 主机，不发起网络请求。
    原文与反转义各扫一遍（覆盖 \\75rl 之类转义混淆）；只有字符串字面量之外、
    处于语法位置的 url(/@import 才计入，content:"…" 内的同形文本不算引用。"""
    def unescape(match):
        codepoint, newline, char = match.groups()
        if codepoint:
            value = int(codepoint, 16)
            return chr(value) if 0 < value <= 0x10FFFF and not 0xD800 <= value <= 0xDFFF else "\ufffd"
        return "" if newline else char

    hosts = set()
    for variant in (text, CSS_ESCAPE_RE.sub(unescape, text)):
        tokens = _css_tokens(variant)
        for idx, (kind, value) in enumerate(tokens):
            if kind != "code":
                continue
            for m in CSS_URL_KW_RE.finditer(value):
                rest = value[m.end():]
                if m.group(0).lower() == "url(":
                    operand = _url_operand(rest, tokens, idx)
                # @import 的字符串操作数：关键字与引号之间只允许空白（含被折算
                # 成空格的注释），如 @import "x" / @import/**/"x"
                elif not rest.strip() and idx + 1 < len(tokens) \
                        and tokens[idx + 1][0] == "string":
                    operand = tokens[idx + 1][1]
                else:
                    operand = ""
                host = _remote_host_of(operand)
                if host:
                    hosts.add(host)
    return hosts


def security_scan(zip_path, cfg, log=print):
    """返回 (是否通过, 发现列表, 统计信息)。发现项为 dict(file, line, sev, desc, text)。"""
    findings = []
    stats = {"files": 0, "scripts": [], "skipped_bat": 0, "css": 0, "fonts": 0,
             "pe": [], "remote_hosts": set()}
    safe_hints = SAFE_SCOPE_HINTS_BASE + (cfg["id"].lower(), cfg["repo"].lower())

    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            findings.append({"file": bad, "line": 0, "sev": "BLOCK",
                             "desc": t("sec_crc_fail"), "text": ""})

        names = zf.namelist()
        stats["files"] = len(names)

        for name in names:  # zip-slip 路径穿越
            if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name) \
                    or ".." in name.replace("\\", "/").split("/"):
                findings.append({"file": name, "line": 0, "sev": "BLOCK",
                                 "desc": t("sec_zip_slip"), "text": ""})

        for name in names:
            if name.endswith("/"):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in SKIPPED_EXTS:
                stats["skipped_bat"] += 1  # 按约定跳过内容分析：本工具不执行 bat/cmd
            elif ext in SCRIPT_EXTS:
                stats["scripts"].append(name)
                for i, line in enumerate(read_zip_text(zf, name).splitlines(), 1):
                    for rule_re, sev, desc_key in SCRIPT_RULES:
                        if re.search(rule_re, line, re.IGNORECASE):
                            findings.append({"file": name, "line": i, "sev": sev,
                                             "desc": t(desc_key), "text": line.strip()[:160]})
                    if DELETE_RE.match(line):
                        low = line.lower()
                        if any(h in low for h in safe_hints):
                            sev, desc = "INFO", t("sec_delete_in_scope")
                        else:
                            sev, desc = "BLOCK", t("sec_delete_out_scope")
                        findings.append({"file": name, "line": i, "sev": sev,
                                         "desc": desc, "text": line.strip()[:160]})
            elif ext in PE_EXTS:
                stats["pe"].append(name)
                findings.append({"file": name, "line": 0, "sev": "WARN",
                                 "desc": t("sec_pe_binary", ext=ext), "text": ""})
            elif ext == ".css":
                stats["css"] += 1
                stats["remote_hosts"].update(css_remote_hosts(read_zip_text(zf, name)))
            elif ext in FONT_EXTS:
                stats["fonts"] += 1

    passed = not any(f["sev"] == "BLOCK" for f in findings)
    return passed, findings, stats


# ------------------------------------------------------------ 主题目录 ----

def detect_themes_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if not base:
            raise RuntimeError(t("err_no_appdata"))
        return Path(base) / "Typora" / "themes"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "abnerworks.Typora" / "themes"
    return Path.home() / ".config" / "Typora" / "themes"


def detect_themes_dir_safe():
    try:
        return detect_themes_dir()
    except Exception:
        return None


# ------------------------------------------------------------ 安装集推断 ----

def infer_cfg_from_zip(files, cfg):
    """对新添加的仓库：依据压缩包内容推断 css_dir / theme_re / assets_dirs 并写回配置。"""
    root_css = [p for p in files if "/" not in p and p.lower().endswith(".css")
                and p not in ROOT_DOC_CSS]
    if root_css:
        css_dir, pool = "", root_css
    else:
        css_dir = None
        for d in ("themes",) + tuple(sorted({p.split("/")[0] for p in files if "/" in p})):
            cand = [p for p in files
                    if p.startswith(d + "/") and p.lower().endswith(".css")]
            if cand:
                css_dir, pool = d, [posixpath.basename(p) for p in cand]
                break
        if css_dir is None:
            raise RuntimeError(t("err_no_css"))
    prefix = os.path.commonprefix(pool)
    dash = prefix.rfind("-")
    prefix = prefix[:dash + 1] if dash > 0 else ""
    cfg["css_dir"] = css_dir
    cfg["theme_re"] = "^" + re.escape(prefix) + "[^/]+\\.css$"
    assets = sorted({p.split("/")[0] for p in files
                     if "/" in p and os.path.splitext(p)[1].lower() in FONT_EXTS
                     and p.split("/")[0] != css_dir})
    cfg["assets_dirs"] = assets
    cfg["inferred"] = True
    return {"css_dir": css_dir, "theme_re": cfg["theme_re"], "assets_dirs": assets}


def install_set_from_zip(files, cfg):
    """计算要写入主题目录的条目: (主题CSS逻辑路径列表, 资源文件逻辑路径列表)。"""
    theme_re = re.compile(cfg["theme_re"])
    css_dir = cfg.get("css_dir") or ""
    css_items = [p for p in files
                 if posixpath.dirname(p) == css_dir and theme_re.match(posixpath.basename(p))]
    asset_items = []
    for pat in cfg.get("assets_dirs") or []:
        pats = (pat.rstrip("/"), pat.rstrip("/") + "/*")
        asset_items += [p for p in files
                        if fnmatch.fnmatch(p, pats[0]) or fnmatch.fnmatch(p, pats[1])]
    return css_items, sorted(set(asset_items))


def bridge_text(cfg):
    b = cfg["bridge"]
    return (t("bridge_comment", name=cfg["name"]) + "\n"
            '@import url("%s");\n' % b["import"])


def bridge_is_ours(themes_dir, cfg):
    b = cfg.get("bridge")
    if not b:
        return False
    f = themes_dir / b["file"]
    if not f.exists():
        return False
    try:
        return posixpath.basename(b["import"]) in f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def new_backup_dir(themes_dir, cfg, suffix=""):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = themes_dir.parent / ("themes-backup-%s-%s%s" % (cfg["id"], stamp, suffix))
    n = 1
    while backup_dir.exists():
        backup_dir = themes_dir.parent / ("themes-backup-%s-%s-%d%s"
                                          % (cfg["id"], stamp, n, suffix))
        n += 1
    backup_dir.mkdir(parents=True)
    return backup_dir


def removal_set(cfg, themes_dir):
    """本仓库在主题目录中的占用文件(相对路径集合): installed.json 记录 ∪ 根目录主题CSS
    ∪ 资源目录 ∪ 属于本仓库的桥接文件。供清空重装/卸载使用。"""
    rels = set()
    installed = load_json_if_exists(repo_dir(cfg) / "installed.json") or {}
    for f in installed.get("files", []):
        rels.add(f)
    theme_re = cfg.get("theme_re")
    if theme_re:
        for p in themes_dir.glob("*.css"):
            if re.match(theme_re, p.name):
                rels.add(p.name)
    for pat in cfg.get("assets_dirs") or []:
        for p in themes_dir.glob(pat.rstrip("/") + "/*"):
            rels.add(p.relative_to(themes_dir).as_posix())
        top = themes_dir / pat.rstrip("/")
        if top.exists() and top.is_file():
            rels.add(pat.rstrip("/"))
    b = cfg.get("bridge")
    if b and (themes_dir / b["file"]).exists() and bridge_is_ours(themes_dir, cfg):
        rels.add(b["file"])
    return sorted(r for r in rels if r and ".." not in r.split("/")
                  and not r.startswith(("/", "\\")))


def backup_and_remove(themes_dir, rels, ensure_backup, log=print):
    """把 rels(相对路径)备份后从主题目录移除。ensure_backup() 惰性创建备份目录。
    返回实际移除的相对路径列表。"""
    removed = []
    for rel in rels:
        src = themes_dir / Path(*rel.split("/"))
        if not src.exists():
            continue
        backup_dir = ensure_backup()
        dst = backup_dir / Path(*rel.split("/"))
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(src, dst, dirs_exist_ok=True)
            shutil.rmtree(src)
        else:
            shutil.copy2(src, dst)
            src.unlink()
        removed.append(rel)
    # 清掉因此变空的资源目录
    for d in sorted({Path(*r.split("/")).parts[0] for r in removed if "/" in r},
                    reverse=True):
        cand = themes_dir / d
        if cand.is_dir() and not any(cand.iterdir()):
            cand.rmdir()
    return removed


def write_install_set(zip_path, top_name, css_items, asset_items, themes_dir):
    """把安装集从压缩包写入主题目录。返回写入的相对路径列表。"""
    wrote = []
    with zipfile.ZipFile(zip_path) as zf:
        name_map = {}
        for zi in zf.infolist():
            if zi.is_dir():
                continue
            rel = zi.filename.replace("\\", "/")
            if top_name:
                rel = rel.split("/", 1)[1] if "/" in rel else None
            if rel:
                name_map[rel] = zi
        for logical in sorted(css_items):
            if logical not in name_map:
                continue
            target = themes_dir / posixpath.basename(logical)
            with zf.open(name_map[logical]) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            wrote.append(target.name)
        for logical in sorted(set(asset_items)):
            if logical not in name_map:
                continue
            target = themes_dir / Path(*logical.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name_map[logical]) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            wrote.append(logical)
    return wrote


# ---------------------------------------------------------------- 报告 ----

class Report:
    def __init__(self, cfg, subtitle=""):
        self.cfg = cfg
        title_key = "report_title_sub" if subtitle else "report_title"
        self.lines = [
            t(title_key, name=cfg["name"], sub=subtitle),
            "",
            t("report_runtime", time=time.strftime("%Y-%m-%d %H:%M:%S")),
            t("report_platform", platform=platform.platform(), py=platform.python_version()),
            t("report_repo", url=github_latest_page(slug(cfg))),
        ]
        self.exit_code = EXIT_OK

    def add(self, text=""):
        self.lines.append(text)

    def section(self, title):
        self.add("## " + title)
        self.add()

    def finish(self, ok_text, exit_code):
        self.section(t("report_conclusion"))
        self.add(ok_text)
        self.exit_code = exit_code

    def save(self, rd):
        rd.mkdir(parents=True, exist_ok=True)
        # 固定文件名(语言无关) + 清理历史变体(含旧版中文名报告)，确保该仓库目录内最多一份报告
        for old in list(rd.glob("install-report*.md")) + list(rd.glob("安装报告*.md")):
            if old.name != REPORT_NAME:
                old.unlink()
        target = rd / REPORT_NAME
        target.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        print(t("report_saved", path=target))


def findings_table(rep, findings):
    merged = {}
    for f in sorted(findings, key=lambda x: (SEV_ORDER[x["sev"]], x["file"], x["line"])):
        key = (f["file"], f["sev"], f["desc"])
        if key in merged:
            merged[key]["n"] += 1
            merged[key]["lines"].append(f["line"])
        else:
            merged[key] = {"n": 1, "lines": [f["line"]], "text": f["text"]}

    rep.add(t("findings_header"))
    rep.add("|---|---|---|---|")
    for (fname, sev, desc), v in merged.items():
        loc = ", ".join(str(x) for x in v["lines"][:8]) + ("…" if v["n"] > 8 else "")
        rep.add("| `%s` | %s | %s | %s |" % (fname, sev, desc,
                                             t("findings_line_no", lines=loc or "-")))


# ------------------------------------------------------------ 主流程: 安装 ----

def run_install(cfg, mode="merge", update_manifest=False, keep_zips=None, log=print,
                approved_remote_hosts=None):
    """完整流水线。返回 (退出码, 结果 dict 供界面展示)。"""
    approved_remote_hosts = sorted({normalize_host(host)
                                    for host in (approved_remote_hosts or [])} - {""})
    cfg_all = load_config()
    if keep_zips is None:
        keep_zips = int(cfg_all["settings"].get("keep_zips", 2))
    rd = repo_dir(cfg)
    rep = Report(cfg, t("sub_merge") if mode == "merge" else t("sub_clean"))
    result = {"ok": False, "exit_code": EXIT_INSTALL, "manifest_mismatch": False,
              "added": [], "removed": [], "message": "",
              "install_options": {"mode": mode, "update_manifest": update_manifest,
                                  "approved_remote_hosts": approved_remote_hosts}}
    try:
        # ---- 1. 获取最新 release ----
        log(t("step_release"))
        meta = fetch_latest_release(cfg, log=log, cache=load_latest_cache(cfg))
        release, meta_source = meta["release"], meta["source"]
        asset = pick_asset(release, cfg)
        tag = release.get("tag_name", "?")
        rel_name = release.get("name", "").strip() or tag
        published = (release.get("published_at") or "").replace("T", " ").replace("Z", " UTC")
        log(t("log_latest", tag=tag, name=rel_name))

        rep.section(t("sec1_title"))
        rep.add(t("sec1_version", tag=tag, name=rel_name))
        rep.add(t("sec1_published", time=published or t("published_unknown")))
        rep.add(t("sec1_source", source=(
            t("source_api_304") if meta["not_modified"]
            else t("source_api") if meta_source == "api"
            else t("source_redirect"))))
        rep.add(t("sec1_asset", name=asset["name"],
                  size=(t("size_mb", mb=int(asset.get("size") or 0) / 1048576)
                        if asset.get("size") else t("size_unknown"))))
        rep.add(t("sec1_url", url=asset["browser_download_url"]))
        rep.add()

        zip_path, downloaded = download_asset(asset, rd / "zips" / tag, log)
        kept, removed_zips = prune_zip_versions(rd, keep_zips, log)
        rep.section(t("sec2_title"))
        rep.add(t("dl_state_new") if downloaded else t("dl_state_skip"))
        rep.add(t("sec2_archive", id=cfg["id"], tag=tag, keep=keep_zips))
        if removed_zips:
            rep.add(t("sec2_pruned", names=", ".join(p.name for p in removed_zips)))
        rep.add()

        # ---- 2. 安全检查 ----
        log(t("step_scan", name=asset["name"]))
        try:
            passed, findings, stats = security_scan(zip_path, cfg, log)
        except zipfile.BadZipFile:
            zip_path.unlink(missing_ok=True)
            raise RuntimeError(t("bad_zip"))
        rep.section(t("sec3_title"))
        rep.add(t("sec3_summary",
                  files=stats["files"], css=stats["css"], fonts=stats["fonts"],
                  scripts=len(stats["scripts"]),
                  script_names=((" (%s)" % ", ".join("`%s`" % os.path.basename(s)
                                                     for s in stats["scripts"]))
                                if stats["scripts"] else ""),
                  bat=stats["skipped_bat"]))
        rep.add(t("sec3_integrity_bad")
                if any(f["sev"] == "BLOCK" and not f["text"] for f in findings)
                else t("sec3_integrity_ok"))
        if stats["remote_hosts"]:
            rep.add(t("sec3_remote", hosts=", ".join(sorted(stats["remote_hosts"]))))
        rep.add()
        if findings:
            findings_table(rep, findings)
        else:
            rep.add(t("sec3_clean"))
        rep.add()
        n_block = sum(1 for f in findings if f["sev"] == "BLOCK")
        n_warn = sum(1 for f in findings if f["sev"] == "WARN")
        if not passed:
            rep.add(t("sec3_fail_line", block=n_block, warn=n_warn))
            log(t("sec3_fail_log", n=n_block))
            msg = t("sec3_fail_msg", n=n_block)
            result.update({"message": msg})
            rep.finish(t("sec3_fail_finish", msg=msg), EXIT_SECURITY)
            result["exit_code"] = EXIT_SECURITY
            return EXIT_SECURITY, result
        rep.add(t("sec3_pass_line", warn=n_warn))
        log(t("sec3_pass_log", n=n_warn))
        rep.add()

        # 同意仅覆盖本次明确列出的主机；重试时如上游增加主机，必须再次确认。
        # 在推断/保存配置、重建基准清单、清空旧主题或写入主题目录之前暂停。
        if stats["remote_hosts"] - set(approved_remote_hosts):
            hosts = sorted(stats["remote_hosts"])
            msg = t("remote_confirm_msg", hosts=", ".join(hosts))
            result.update({"remote_confirmation_required": True, "remote_hosts": hosts,
                           "version": tag, "exit_code": EXIT_CONFIRM, "message": msg})
            log(msg)
            log(t("remote_confirm_cli"))
            rep.finish("⚠️ " + msg, EXIT_CONFIRM)
            return EXIT_CONFIRM, result

        # ---- 3. 结构推断 / 清单比对 ----
        zip_files, top_name = zip_logical_files(zip_path)
        if not cfg.get("theme_re"):
            log(t("step_infer"))
            inferred = infer_cfg_from_zip(zip_files, cfg)
            cfg_all["repos"][cfg["id"]] = cfg
            save_config(cfg_all)
            rep.section(t("sec4_title"))
            rep.add(t("sec4_inferred",
                      css_dir=inferred["css_dir"] or t("root_dir_label"),
                      theme_re=inferred["theme_re"],
                      assets=", ".join("`%s`" % a for a in inferred["assets_dirs"])
                      or t("none")))
            rep.add()
            log(t("infer_log",
                  css_dir=inferred["css_dir"] or t("root_dir_log"),
                  theme_re=inferred["theme_re"], assets=inferred["assets_dirs"]))
        else:
            log(t("step_manifest"))
        rep.section(t("sec5_title"))
        manifest_path = rd / "manifest.json"
        rep.add(t("sec5_manifest", name=manifest_path.name))
        manifest = load_manifest(rd)
        if update_manifest:
            save_manifest(rd, zip_files, tag)
            rep.add(t("sec5_rebuilt", tag=tag, n=len(zip_files)))
            manifest = (set(zip_files), tag)
        elif manifest is None:
            save_manifest(rd, zip_files, tag)
            manifest = (set(zip_files), tag)
            rep.add(t("sec5_created", tag=tag, n=len(zip_files)))
            log(t("sec5_created_log", n=len(zip_files)))
        expected, base_ver = manifest
        added = sorted(set(zip_files) - expected)
        removed = sorted(expected - set(zip_files))
        if added or removed:
            rep.add(t("sec5_mismatch", ver=base_ver, n=len(expected)))
            if added:
                rep.add(t("sec5_added", n=len(added),
                          files=", ".join("`%s`" % f for f in added)))
            if removed:
                rep.add(t("sec5_removed", n=len(removed),
                          files=", ".join("`%s`" % f for f in removed)))
            rep.add()
            log(t("sec5_mismatch_log", n=len(added), m=len(removed)))
            result.update({"manifest_mismatch": True, "added": added, "removed": removed,
                           "version": tag, "exit_code": EXIT_MANIFEST,
                           "message": t("mismatch_msg")})
            rep.finish(t("mismatch_finish", id=cfg["id"]), EXIT_MANIFEST)
            return EXIT_MANIFEST, result
        rep.add(t("sec5_match", ver=base_ver, n=len(expected)))
        log(t("sec5_match_log", n=len(expected)))
        rep.add()

        # ---- 4. 安装 ----
        log(t("step_install"))
        themes_dir = detect_themes_dir()
        themes_dir.mkdir(parents=True, exist_ok=True)
        log(t("themes_dir_log", dir=themes_dir))
        rep.section(t("sec6_title"))
        rep.add(t("sec6_dir", dir=themes_dir))
        css_items, asset_items = install_set_from_zip(zip_files, cfg)
        if not css_items:
            msg = t("no_theme_css_msg", re=cfg["theme_re"])
            result.update({"message": msg, "exit_code": EXIT_INSTALL})
            rep.add("❌ %s" % msg)
            rep.finish(t("install_fail_finish", msg=msg), EXIT_INSTALL)
            return EXIT_INSTALL, result

        wrote = []
        backup_state = {"dir": None}

        def ensure_backup():
            if backup_state["dir"] is None:
                backup_state["dir"] = new_backup_dir(themes_dir, cfg)
            return backup_state["dir"]

        if mode == "clean":
            log(t("clean_log"))
            rels = removal_set(cfg, themes_dir)
            removed_now = backup_and_remove(themes_dir, rels, ensure_backup, log) if rels else []
            if removed_now:
                rep.add(t("clean_done", n=len(removed_now),
                          dir=backup_state["dir"].name))
                wrote.extend(removed_now)  # 记录中保留这些路径以便后续卸载仍然完整
                log(t("clean_done_log", n=len(removed_now),
                      dir=backup_state["dir"].name))
            else:
                rep.add(t("clean_nothing"))

        wrote += write_install_set(zip_path, top_name, css_items, asset_items, themes_dir)
        css_dir_label = (cfg.get("css_dir") or t("root_dir_label"))
        assets_label = ", ".join("`%s`" % a for a in (cfg.get("assets_dirs") or [])) or t("none")
        rep.add(t("sec6_installed", n=len(css_items), css_dir=css_dir_label,
                  assets=assets_label, m=len(asset_items)))
        rep.add(t("sec6_skipped",
                  n=len(zip_files) - len(css_items) - len(asset_items)))

        bridge_done = bridge_saved = False
        b = cfg.get("bridge")
        if b:
            if (themes_dir / b["requires"]).exists():
                bridge_file = themes_dir / b["file"]
                if bridge_file.exists() and not bridge_is_ours(themes_dir, cfg):
                    shutil.copy2(bridge_file, ensure_backup() / (b["file"] + ".bak"))
                    bridge_saved = True
                bridge_file.write_text(bridge_text(cfg), encoding="utf-8")
                wrote.append(b["file"])
                bridge_done = True
                rep.add(t("bridge_done", file=b["file"], target=b["import"],
                          saved=t("bridge_saved_suffix") if bridge_saved else ""))
            else:
                rep.add(t("bridge_missing", theme=b["requires"]))
        rep.add()

        # ---- 5. 验证与记录 ----
        log(t("step_verify"))
        theme_re = re.compile(cfg["theme_re"])
        installed_themes = sorted(p.name for p in themes_dir.glob("*.css")
                                  if theme_re.match(p.name))
        rep.section(t("sec7_title"))
        min_themes = cfg.get("min_themes")
        if min_themes is None:  # 首次安装以本次安装集为准回填门槛
            min_themes = len(css_items)
            cfg["min_themes"] = min_themes
            cfg_all["repos"][cfg["id"]] = cfg
            save_config(cfg_all)
        rep.add(t("sec7_count", re=cfg["theme_re"], n=len(installed_themes), min=min_themes))
        if installed_themes:
            rep.add(t("sec7_themes", names=", ".join(n[:-4] for n in installed_themes)))
        if b:
            rep.add(t("sec7_bridge", file=b["file"],
                      state=t("bridge_generated") if bridge_done else t("bridge_not_generated")))
        backups = sorted(p.name for p in themes_dir.parent.glob("themes-backup-%s-*" % cfg["id"]))
        if backups:
            rep.add(t("sec7_backups", names=", ".join(backups[-3:])))
        rep.add()

        # ---- 6. 落盘记录 ----
        log(t("step_record"))
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "installed.json").write_text(json.dumps({
            "version": tag,
            "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode,
            "files": sorted(set(wrote)),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        if len(installed_themes) >= min_themes and installed_themes:
            msg = t("install_ok_msg", tag=tag, n=len(installed_themes), name=cfg["name"])
            rep.finish("✅ " + msg, EXIT_OK)
            log(t("install_ok_log", n=len(installed_themes)))
            result.update({"ok": True, "exit_code": EXIT_OK, "version": tag,
                           "themes": len(installed_themes), "message": msg})
            return EXIT_OK, result
        msg = t("install_insufficient", n=len(installed_themes), min=min_themes)
        rep.finish("⚠️ " + msg, EXIT_INSTALL)
        result.update({"exit_code": EXIT_INSTALL, "version": tag,
                       "themes": len(installed_themes), "message": msg})
        return EXIT_INSTALL, result

    except Exception as exc:  # 任何异常也落一份报告
        rep.section(t("report_error_section"))
        rep.add("❌ %s" % exc)
        msg = t("run_aborted", exc=exc)
        result.update({"message": msg, "exit_code": EXIT_DOWNLOAD})
        rep.finish("❌ " + msg, EXIT_DOWNLOAD)
        log(t("err_log", exc=exc))
        return EXIT_DOWNLOAD, result
    finally:
        try:
            rep.save(rd)
        except Exception as exc:
            log(t("report_save_failed", exc=exc))


# ------------------------------------------------------------ 卸载 ----

def run_uninstall(cfg, log=print):
    """卸载本仓库主题：installed.json 记录 ∪ 模式匹配的根CSS ∪ 资源目录 ∪ 本仓库桥接，
    全部先备份到主题目录同级再移除。"""
    rd = repo_dir(cfg)
    rep = Report(cfg, t("sub_uninstall"))
    result = {"ok": False, "exit_code": EXIT_INSTALL, "message": ""}
    try:
        themes_dir = detect_themes_dir()
        rels = removal_set(cfg, themes_dir) if themes_dir.exists() else []
        rep.section(t("uninstall_detail"))
        rep.add(t("uninstall_dir", dir=themes_dir))
        if not rels:
            msg = t("uninstall_nothing_msg")
            rep.add(t("uninstall_nothing_line", msg=msg))
            rep.finish(t("uninstall_nothing_finish", msg=msg), EXIT_OK)
            result.update({"ok": True, "exit_code": EXIT_OK, "message": msg})
            log("  %s" % msg)
            return EXIT_OK, result
        log(t("uninstall_backup_log", n=len(rels)))
        backup_dir = new_backup_dir(themes_dir, cfg, "-uninstall")
        removed = backup_and_remove(themes_dir, rels, lambda: backup_dir, log)
        rep.add(t("uninstall_removed", n=len(removed), dir=backup_dir.name))
        for r in removed[:30]:
            rep.add("  - `%s`" % r)
        if len(removed) > 30:
            rep.add(t("uninstall_more", n=len(removed)))
        (rd / "installed.json").unlink(missing_ok=True)
        msg = t("uninstall_ok_msg", name=cfg["name"], n=len(removed), dir=backup_dir.name)
        rep.finish("✅ " + msg, EXIT_OK)
        result.update({"ok": True, "exit_code": EXIT_OK, "message": msg})
        log(t("uninstall_ok_log", n=len(removed)))
        return EXIT_OK, result
    except Exception as exc:
        rep.section(t("report_error_section"))
        rep.add("❌ %s" % exc)
        result.update({"message": t("uninstall_fail_msg", exc=exc)})
        rep.finish(t("uninstall_fail_finish", exc=exc), EXIT_INSTALL)
        log(t("err_log", exc=exc))
        return EXIT_INSTALL, result
    finally:
        try:
            rep.save(rd)
        except Exception as exc:
            log(t("report_save_failed", exc=exc))


# ------------------------------------------------------------ 版本检查 ----

def run_check(cfg, log=print):
    """查询最新 release 并缓存到 repos/<id>/latest.json（完整投影，支持 ETag 304 复用）。
    返回 (退出码, 结果)。"""
    rd = repo_dir(cfg)
    result = {"ok": False, "exit_code": EXIT_DOWNLOAD, "message": ""}
    cache = load_latest_cache(cfg)
    try:
        meta = fetch_latest_release(cfg, log=log, cache=cache)
        release, meta_source = meta["release"], meta["source"]
        asset = pick_asset(release, cfg)
        tag = release.get("tag_name", "?")
        latest = {
            "tag": tag,
            "name": (release.get("name") or "").strip() or tag,
            "published_at": release.get("published_at", ""),
            "asset": asset["name"],
            "asset_size": int(asset.get("size") or 0),
            "asset_url": asset.get("browser_download_url")
                         or github_download_url(slug(cfg), tag, asset["name"]),
            "meta_source": meta_source,
            "etag": meta.get("etag") or "",
            "asset_pattern": (meta["asset_pattern"] if meta.get("asset_pattern") is not None
                              else cache.get("asset_pattern")),
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n",
                                        encoding="utf-8")
        installed = load_json_if_exists(rd / "installed.json") or {}
        cur = installed.get("version")
        if cur:
            state = (t("state_uptodate") if cur == tag
                     else (t("state_newer", tag=tag, cur=cur)
                           if version_key(tag) > version_key(cur)
                           else t("state_local_newer", cur=cur, tag=tag)))
            log(t("check_state_log", id=cfg["id"], cur=cur, tag=tag, state=state))
        else:
            state = t("state_not_installed")
            log(t("check_latest_log", id=cfg["id"], tag=tag))
        result.update({"ok": True, "exit_code": EXIT_OK, "version": tag, "current": cur,
                       "message": t("check_ok_msg", id=cfg["id"], tag=tag, state=state)})
        return EXIT_OK, result
    except Exception as exc:
        msg = t("check_fail_msg", exc=exc)
        log(t("check_fail_log", id=cfg["id"], msg=msg))
        # 失败时保留旧缓存的 tag/etag 等字段（仅更新 error 与时间），下次仍可 304 复用
        latest = dict(cache)
        latest["error"] = str(exc)
        latest["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            rd.mkdir(parents=True, exist_ok=True)
            (rd / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")
        except Exception:
            pass
        result.update({"message": msg})
        return EXIT_DOWNLOAD, result


def run_repos_parallel(cfgs, runner, verb, log=print):
    """并行对多个仓库执行 runner(cfg, log)。各仓库日志先缓存进独立缓冲、按完成顺序
    整段输出，避免并行输出与 stdout 捕获机制交错。返回 (退出码, 汇总结果)。"""
    if not cfgs:
        return EXIT_OK, {"ok": True, "exit_code": EXIT_OK, "message": t("parallel_none")}

    def worker(c):
        lines = []
        rc, res = runner(c, log=lambda msg="", **kw: lines.append(msg))
        return c, rc, res, lines

    outcomes = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(cfgs))) as pool:
        futs = [pool.submit(worker, c) for c in cfgs]
        for fut in concurrent.futures.as_completed(futs):
            c, rc, res, lines = fut.result()
            outcomes.append((rc, res))
            log(t("parallel_running", verb=verb, slug=slug(c)))
            for line in lines:
                log(line)
    final_rc = max(rc for rc, _ in outcomes)
    n_fail = sum(1 for rc, _ in outcomes if rc != EXIT_OK)
    n_adopt = sum(1 for _, res in outcomes if res.get("adopted"))
    msg = t("parallel_summary", verb=verb, n=len(outcomes),
            fail=(t("parallel_fail_part", n=n_fail) if n_fail else t("parallel_all_ok")),
            adopt=(t("parallel_adopt_part", n=n_adopt) if n_adopt else ""))
    log(msg)
    return final_rc, {"ok": final_rc == EXIT_OK, "exit_code": final_rc, "message": msg}


def check_repos_parallel(cfgs, log=print):
    """并行检查多个仓库的最新版本。"""
    return run_repos_parallel(cfgs, run_check, t("verb_check"), log=log)


# ------------------------------------------------------------ 本地安装识别 ----

def present_footprint(cfg, themes_dir):
    """扫描主题目录，返回本仓库当前实际存在的文件（相对路径，已排序）与其中根目录
    主题 CSS 的数量。判定依据 theme_re / assets_dirs / 属于本仓库的桥接文件，
    不依赖 installed.json——用于识别「主题已装但登记丢失」的场景。"""
    rels = set()
    n_css = 0
    theme_re = cfg.get("theme_re")
    if theme_re:
        for p in themes_dir.glob("*.css"):
            if re.match(theme_re, p.name):
                rels.add(p.name)
                n_css += 1
    for pat in cfg.get("assets_dirs") or []:
        top = pat.rstrip("/")
        for p in themes_dir.glob(top + "/*"):
            rels.add(p.relative_to(themes_dir).as_posix())
        single = themes_dir / top
        if single.exists() and single.is_file():
            rels.add(top)
    b = cfg.get("bridge")
    if b and bridge_is_ours(themes_dir, cfg):
        rels.add(b["file"])
    return sorted(r for r in rels if r and ".." not in r.split("/")
                  and not r.startswith(("/", "\\"))), n_css


def infer_cfg_from_local(cfg, log=print):
    """离线推断安装结构：依次尝试本地基准清单 → 最新本地归档 zip → 旧安装记录的
    文件清单，成功则写回配置。返回 (文件清单或 None, 版本线索或 None)。"""
    rd = repo_dir(cfg)
    files = tag = None
    src = ""
    data = load_json_if_exists(rd / "manifest.json")
    if data and data.get("files"):
        files, tag, src = data["files"], data.get("version"), t("src_manifest")
    if files is None and (rd / "zips").is_dir():
        vers = sorted((p for p in (rd / "zips").iterdir() if p.is_dir()),
                      key=lambda p: (version_key(p.name), p.name), reverse=True)
        for v in vers:
            archived = sorted(v.glob("*.zip"))
            if not archived:
                continue
            try:
                files, _top = zip_logical_files(archived[0])
            except Exception:
                continue
            tag, src = v.name, t("src_zip_archive", tag=v.name)
            break
    if files is None:
        old = load_json_if_exists(rd / "installed.json") or {}
        if old.get("files"):
            files, tag, src = old["files"], old.get("version"), t("src_installed")
    if files is None:
        return None, None
    try:
        infer_cfg_from_zip(files, cfg)
    except RuntimeError as exc:
        log(t("infer_local_fail", src=src, exc=exc))
        return None, None
    update_repo_cfg(cfg)
    log(t("infer_local_restored", src=src,
          css_dir=cfg["css_dir"] or t("root_dir_label"),
          theme_re=cfg["theme_re"], assets_dirs=cfg.get("assets_dirs")))
    return files, tag


def _guess_installed_version(cfg, rd, present, zip_files=None, zip_tag=None, log=print):
    """推断既有安装的版本号（只看文件路径齐整度，不校验内容）：优先本次下载的
    release 资产清单，其次本地基准清单；路径不吻合时返回 None（版本未知）。"""
    if not cfg.get("theme_re"):
        return None
    sources = []
    if zip_files is not None:
        sources.append((zip_files, zip_tag or t("unknown"), t("src_release_asset")))
    mdata = load_json_if_exists(rd / "manifest.json")
    if mdata and mdata.get("files"):
        sources.append((mdata["files"], mdata.get("version") or t("unknown"),
                        t("src_local_manifest")))
    present_set = set(present)
    for files, ver, label in sources:
        try:
            css_items, asset_items = install_set_from_zip(files, cfg)
        except (re.error, KeyError, TypeError):
            continue
        want = {posixpath.basename(c) for c in css_items} | set(asset_items)
        if not want:
            continue
        missing = sorted(want - present_set)
        if missing:
            log(t("version_compare_missing",
                  label=label, ver=ver, n=len(missing),
                  files=", ".join(missing[:5]),
                  more=t("more_suffix") if len(missing) > 5 else ""))
            continue
        return None if ver == t("unknown") else ver
    return None


def try_adopt(cfg, zip_files=None, zip_tag=None, log=print):
    """识别既有安装：主题目录中本仓库主题文件实际存在而 installed.json 缺失/过期时，
    按实际情况（重）写安装记录——只登记，绝不改动主题目录文件；若带 zip 清单且本地
    缺基准清单，顺带建立基准。返回 (是否识别为已安装, 消息)。"""
    themes_dir = detect_themes_dir_safe()
    if not themes_dir or not themes_dir.is_dir():
        return False, t("no_themes_dir")
    present, n_css = present_footprint(cfg, themes_dir)
    if n_css == 0:
        return False, t("no_matching_css", re=cfg.get("theme_re"))
    rd = repo_dir(cfg)
    old = load_json_if_exists(rd / "installed.json") or {}
    version = old.get("version")
    if version is None:
        version = _guess_installed_version(cfg, rd, present, zip_files, zip_tag, log)
    if cfg.get("min_themes") is None:
        cfg["min_themes"] = n_css
        update_repo_cfg(cfg)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "installed.json").write_text(json.dumps({
        "version": version,
        "installed_at": old.get("installed_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": old.get("mode") or "adopted",
        "files": present,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if zip_files is not None and cfg.get("theme_re") and not (rd / "manifest.json").exists():
        save_manifest(rd, zip_files, zip_tag or t("unknown"))
        log(t("adopt_manifest_created", tag=zip_tag or "?", n=len(zip_files)))
    msg = t("adopt_ok_msg", n=n_css, m=len(present), ver=version or t("ver_unknown"))
    log("  " + msg)
    return True, msg


def run_rescan(cfg, log=print):
    """重新索引单仓库：① 刷新最新版本缓存（网络，失败不阻断本地识别）；
    ② 确定安装结构（已配置 → 本地遗留数据 → 下载 release 资产推断）；
    ③ 扫描主题目录识别既有安装（只登记，不改动主题目录）。
    返回 (退出码, 结果 dict，含 adopted 标记)。"""
    rd = repo_dir(cfg)
    result = {"ok": False, "exit_code": EXIT_DOWNLOAD, "message": "", "adopted": False}
    zip_files = zip_tag = None
    try:
        log(t("rescan_step1"))
        check_ok = True
        try:
            rc_chk, _res = run_check(cfg, log=log)
            check_ok = rc_chk == EXIT_OK
        except Exception as exc:
            check_ok = False
            log(t("rescan_check_exc", exc=exc))
        if not check_ok:
            log(t("rescan_check_failed"))

        if not cfg.get("theme_re"):
            log(t("rescan_step2"))
            zip_files, zip_tag = infer_cfg_from_local(cfg, log)
        if not cfg.get("theme_re"):
            latest = load_latest_cache(cfg)
            if not (latest.get("tag") and latest.get("asset")):
                raise RuntimeError(t("rescan_no_structure"))
            log(t("rescan_download_infer", asset=latest["asset"]))
            asset = {"name": latest["asset"], "size": int(latest.get("asset_size") or 0),
                     "browser_download_url": latest.get("asset_url")
                     or github_download_url(slug(cfg), latest["tag"], latest["asset"])}
            zip_path, _new = download_asset(asset, rd / "zips" / latest["tag"], log)
            prune_zip_versions(rd, int(load_config()["settings"].get("keep_zips", 2)), log)
            zip_files, _top = zip_logical_files(zip_path)
            zip_tag = latest["tag"]
            infer_cfg_from_zip(zip_files, cfg)
            update_repo_cfg(cfg)
            log(t("rescan_inferred_log", re=cfg["theme_re"]))

        log(t("rescan_step3"))
        adopted, msg = try_adopt(cfg, zip_files=zip_files, zip_tag=zip_tag, log=log)
        result["adopted"] = adopted
        if adopted or check_ok:
            result.update({"ok": True, "exit_code": EXIT_OK,
                           "message": msg if adopted else t("rescan_ok_with_check", msg=msg)})
            return EXIT_OK, result
        result.update({"message": msg})
        return EXIT_DOWNLOAD, result
    except Exception as exc:
        msg = t("rescan_fail_msg", exc=exc)
        log("  " + msg)
        result.update({"message": msg})
        return EXIT_DOWNLOAD, result


# ------------------------------------------------------------ 仓库增删 ----

def add_repo(url, name=None, log=print):
    """从 GitHub URL / owner/repo 添加仓库。添加时先检索本地：repos/<id>/ 有遗留数据
    （此前移除未清空）或 Typora 主题目录中已存在本仓库主题文件的，直接恢复结构配置
    与安装记录（纯本地、不联网、不改动主题目录）。"""
    url = (url or "").strip()
    m = re.search(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/#?].*)?$",
                  url) or re.match(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$", url)
    if not m:
        raise RuntimeError(t("err_parse_url", url=url))
    owner, repo = m.group(1), m.group(2)
    repo_id = re.sub(r"[^a-z0-9-]+", "-", repo.lower()).strip("-") or "repo"
    if repo_id == "all":
        repo_id = repo_id + "-theme"
    cfg = {
        "id": repo_id,
        "name": name or repo,
        "owner": owner,
        "repo": repo,
        "asset_re": None,
        "css_dir": None,
        "theme_re": None,
        "assets_dirs": None,
        "bridge": None,
        "min_themes": None,
        "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with CONFIG_LOCK:
        cfg_all = load_config()
        if repo_id in cfg_all["repos"]:
            raise RuntimeError(t("err_already_registered", repo=repo, id=repo_id))
        cfg_all["repos"][repo_id] = cfg
        save_config(cfg_all)
    rd = repo_dir(cfg)
    if rd.is_dir() and any(rd.iterdir()):
        log(t("add_legacy_found", id=repo_id))
    log(t("add_added", slug=slug(cfg), id=repo_id))
    if not cfg.get("theme_re"):
        infer_cfg_from_local(cfg, log)  # 遗留 manifest/zip/安装记录 → 恢复结构配置
    if cfg.get("theme_re"):
        try_adopt(cfg, log=log)  # 主题目录已有本仓库主题 → 直接恢复为已安装
    else:
        log(t("add_no_clue"))
    return cfg


def remove_repo(repo_id, purge=False, log=print):
    with CONFIG_LOCK:
        cfg_all = load_config()
        cfg = find_repo(cfg_all, repo_id)
        del cfg_all["repos"][repo_id]
        save_config(cfg_all)
    if purge:
        shutil.rmtree(repo_dir(cfg), ignore_errors=True)
        log(t("remove_purged", slug=slug(cfg), id=repo_id))
    else:
        log(t("remove_kept", slug=slug(cfg), id=repo_id))
    return cfg


# ------------------------------------------------------------ 状态汇总 ----

def repo_overview(cfg, themes_dir=None, root_css=None):
    """汇总单仓库状态。themes_dir/root_css 由调用方统一探测一次后传入，
    避免逐仓库重复探测主题目录与枚举根 CSS。"""
    rd = repo_dir(cfg)
    installed = load_json_if_exists(rd / "installed.json")
    latest = load_json_if_exists(rd / "latest.json")
    if root_css is None:
        themes_dir = themes_dir or detect_themes_dir_safe()
        if themes_dir and themes_dir.is_dir():
            root_css = [p.name for p in themes_dir.glob("*.css")]
        else:
            root_css = []
    n = sum(1 for name in root_css if re.match(cfg["theme_re"], name)) \
        if (cfg.get("theme_re") and root_css) else 0
    return {
        "id": cfg["id"], "name": cfg["name"], "slug": slug(cfg),
        "url": github_latest_page(slug(cfg)),
        "installed_version": (installed or {}).get("version"),
        "installed_at": (installed or {}).get("installed_at"),
        "has_record": bool(installed),
        "themes_present": n,
        "latest": latest,
        "up_to_date": bool(installed and latest and not latest.get("error")
                           and installed.get("version") == latest.get("tag")),
        "has_report": (rd / REPORT_NAME).exists(),
        "auto_inferred": bool(cfg.get("inferred")),
    }


# ------------------------------------------------------------ 网页界面 ----

class JobManager:
    """全局单任务: 安装/卸载/检查 一次只跑一个，stdout 被捕获为任务日志。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.job = None
        self.seq = 0  # 任务序号(单调递增)：前端据此保证每个任务的结果只处理一次

    def busy(self):
        return bool(self.job and self.job["running"])

    def start(self, kind, repo_id, fn):
        with self.lock:
            if self.busy():
                return False
            self.seq += 1
            self.job = {"seq": self.seq, "kind": kind, "repo": repo_id, "running": True,
                        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "log": [], "result": None}
            job = self.job
        threading.Thread(target=self._run, args=(job, fn), daemon=True).start()
        return True

    def _run(self, job, fn):
        parts = job["log"]  # Capture 直接写入任务的日志列表

        class Capture:
            def write(self, s):
                if s.strip() or s == "\n":
                    parts.append(s)
                return len(s)

            def flush(self):
                pass

        def log(msg="", **kw):  # 引擎的 log 参数在这里输出到捕获缓冲
            end = kw.get("end", "\n")
            print(msg, end=end)

        cap = Capture()
        try:
            with contextlib.redirect_stdout(cap), contextlib.redirect_stderr(cap):
                rc, result = fn(log)
            job["result"] = result or {"exit_code": rc}
        except Exception as exc:
            job["result"] = {"ok": False, "exit_code": 1, "message": t("internal_error", exc=exc)}
        finally:
            job["running"] = False

    def snapshot(self):
        job = self.job
        if not job:
            return {"seq": self.seq, "running": False, "log": [], "result": None}
        text = "".join(job["log"])
        lines = [l for l in text.replace("\r", "\n").split("\n") if l.strip()]
        return {"seq": job["seq"], "running": job["running"], "kind": job["kind"],
                "repo": job["repo"], "started_at": job["started_at"],
                "log": lines[-600:], "result": job["result"]}


JOBS = JobManager()


class UiHandler(http.server.BaseHTTPRequestHandler):
    server_version = "TyporaThemeManager/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静模式
        pass

    # ---- 基础 ----

    def _host_ok(self):
        port = self.server.server_address[1]
        return self.headers.get("Host") in ("127.0.0.1:%d" % port, "localhost:%d" % port,
                                            "[::1]:%d" % port)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _guard_mutation(self):
        """仅允许本机 + 带自定义头的请求变更数据（阻止浏览器跨站驱动本地接口）。"""
        if not self._host_ok():
            self._json({"error": t("err_bad_host")}, 403)
            return False
        if self.headers.get("X-Local-Tool") != "1":
            self._json({"error": t("err_no_header")}, 403)
            return False
        return True

    # ---- 路由 ----

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if not self._host_ok():
            return self._json({"error": t("err_bad_host")}, 403)
        if path in ("/", "/index.html"):
            return self._serve_ui()
        if path == "/api/state":
            cfg_all = load_config()
            themes_dir = detect_themes_dir_safe()
            root_css = ([p.name for p in themes_dir.glob("*.css")]
                        if themes_dir and themes_dir.is_dir() else [])
            return self._json({
                "themes_dir": str(themes_dir or ""),
                "busy": JOBS.busy(),
                "keep_zips": cfg_all["settings"].get("keep_zips", 2),
                "lang": CURRENT_LANG,
                "repos": [repo_overview(c, themes_dir, root_css)
                          for c in cfg_all["repos"].values()],
            })
        if path == "/api/job":
            return self._json(JOBS.snapshot())
        m = re.match(r"^/api/repos/([^/]+)/report$", path)
        if m:
            try:
                cfg = find_repo(load_config(), m.group(1))
            except RuntimeError as exc:
                return self._json({"error": str(exc)}, 404)
            rp = repo_dir(cfg) / REPORT_NAME
            if not rp.exists():
                return self._json({"error": t("err_no_report")}, 404)
            return self._json({"name": cfg["name"],
                               "markdown": rp.read_text(encoding="utf-8")})
        return self._json({"error": t("err_not_found")}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._guard_mutation():
            return
        try:
            body = self._read_json()
            if path == "/api/lang":
                raw = (body.get("lang") or "").strip()
                if raw not in LANGS:
                    return self._json({"error": t("err_lang_invalid",
                                                  langs="/".join(LANGS))}, 400)
                return self._json({"ok": True, "lang": set_lang(raw, persist=True)})
            if path == "/api/repos":
                cfg = add_repo(body.get("url", ""), body.get("name") or None)
                return self._json({"ok": True, "repo": repo_overview(cfg)})
            m = re.match(r"^/api/repos/([^/]+)/(check|install|uninstall|rescan)$", path)
            if m:
                repo_id, action = m.group(1), m.group(2)
                if repo_id == "all" and action in ("check", "rescan"):
                    def run_all(log):
                        repos = list(load_config()["repos"].values())
                        if action == "check":
                            return check_repos_parallel(repos, log=log)
                        return run_repos_parallel(repos, run_rescan, t("verb_rescan"), log=log)
                    if not JOBS.start(action + "-all", "all", run_all):
                        return self._json({"error": t("err_busy")}, 409)
                    return self._json({"ok": True, "started": action + "-all"})
                cfg = find_repo(load_config(), repo_id)
                if action == "check":
                    fn = lambda log: run_check(cfg, log=log)
                elif action == "rescan":
                    fn = lambda log: run_rescan(cfg, log=log)
                elif action == "uninstall":
                    fn = lambda log: run_uninstall(cfg, log=log)
                else:
                    mode = body.get("mode", "merge")
                    if mode not in ("merge", "clean"):
                        return self._json({"error": t("err_bad_mode")}, 400)
                    approved_hosts = body.get("approved_remote_hosts", [])
                    if not isinstance(approved_hosts, list) or any(not isinstance(h, str) for h in approved_hosts):
                        return self._json({"error": t("err_remote_hosts")}, 400)
                    fn = lambda log: run_install(cfg, mode=mode,
                                                 update_manifest=bool(body.get("update_manifest")),
                                                 approved_remote_hosts=approved_hosts,
                                                 log=log)
                if not JOBS.start(action, repo_id, fn):
                    return self._json({"error": t("err_busy")}, 409)
                return self._json({"ok": True, "started": action})
        except (RuntimeError, ValueError) as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"error": t("err_not_found")}, 404)

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        purge = "purge=1" in (self.path.split("?", 1) + [""])[1]
        if not self._guard_mutation():
            return
        m = re.match(r"^/api/repos/([^/]+)$", path)
        if not m:
            return self._json({"error": t("err_not_found")}, 404)
        try:
            remove_repo(m.group(1), purge=purge)
        except RuntimeError as exc:
            return self._json({"error": str(exc)}, 404)
        return self._json({"ok": True})

    def _serve_ui(self):
        try:
            body = UI_FILE.read_bytes()
        except OSError:
            body = ("<h1>%s</h1><p>%s</p>" % (t("err_ui_missing_title"),
                                              t("err_ui_missing_body"))).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def run_ui(port=DEFAULT_PORT, no_open=False):
    ensure_default_config()  # 首次运行生成默认 repos.json（变更类命令另经 save_config 落盘）
    chosen = None
    for p in range(port, port + 20):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                chosen = p
                break
            except OSError:
                continue
    if chosen is None:
        raise RuntimeError(t("err_ports_busy", lo=port, hi=port + 19))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", chosen), UiHandler)
    url = "http://127.0.0.1:%d/" % chosen
    print(t("ui_started", url=url))
    if not no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n" + t("ui_exited"))
    finally:
        httpd.server_close()


# ---------------------------------------------------------------- CLI ----

def _setup_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True, errors="replace")
        except Exception:
            pass


def cmd_list(_args):
    cfg_all = load_config()
    themes_dir = detect_themes_dir_safe()
    root_css = ([p.name for p in themes_dir.glob("*.css")]
                if themes_dir and themes_dir.is_dir() else [])
    print(t("cli_themes_dir", dir=themes_dir or t("cli_themes_dir_none")))
    print(t("cli_registered", n=len(cfg_all["repos"])))
    if not cfg_all["repos"]:
        print(t("cli_empty_hint", prog=Path(sys.argv[0]).name))
        return EXIT_OK
    for ov in (repo_overview(c, themes_dir, root_css) for c in cfg_all["repos"].values()):
        if ov["installed_version"]:
            state = t("cli_state_installed", ver=ov["installed_version"],
                      n=ov["themes_present"])
        elif ov["has_record"]:
            state = t("cli_state_adopted", n=ov["themes_present"])
        elif ov["themes_present"]:
            state = t("cli_state_unregistered", n=ov["themes_present"])
        else:
            state = t("cli_state_not_installed")
        latest = ov["latest"]
        if latest and not latest.get("error"):
            state += t("cli_latest_suffix", tag=latest["tag"])
            if ov["up_to_date"]:
                state += t("cli_uptodate_suffix")
        elif latest:
            state += t("cli_check_failed_suffix", err=latest["error"])
        print("  %-22s %-40s %s" % (ov["id"], ov["slug"], state))
    return EXIT_OK


def cmd_check(args):
    cfg_all = load_config()
    ids = args.ids or list(cfg_all["repos"])
    # 先全部校验 id，避免查到一半才发现写错
    cfgs = [find_repo(cfg_all, i) for i in ids]
    rc, _ = check_repos_parallel(cfgs, log=print)
    return rc


def cmd_rescan(args):
    cfg_all = load_config()
    ids = args.ids or list(cfg_all["repos"])
    cfgs = [find_repo(cfg_all, i) for i in ids]
    rc, _ = run_repos_parallel(cfgs, run_rescan, t("verb_rescan"), log=print)
    return rc


def cmd_install(args):
    cfg = find_repo(load_config(), args.id)
    rc, result = run_install(cfg, mode=args.mode,
                             update_manifest=args.update_manifest,
                             keep_zips=args.keep_zips,
                             approved_remote_hosts=args.allow_remote_host)
    if result.get("message"):
        print(result["message"])
    return rc


def cmd_uninstall(args):
    cfg = find_repo(load_config(), args.id)
    rc, result = run_uninstall(cfg)
    if result.get("message"):
        print(result["message"])
    return rc


def cmd_add(args):
    add_repo(args.url, args.name)
    return EXIT_OK


def cmd_remove(args):
    remove_repo(args.id, purge=args.purge)
    return EXIT_OK


def main():
    # --lang 预扫描：argparse 处理 --help 时即退出，语言必须先于 parser 初始化，
    # 否则 `--lang en-US --help` 的帮助文本仍是默认语言
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--lang", default=None)
    pre_args, _ = pre.parse_known_args()
    init_lang(pre_args.lang)

    parser = argparse.ArgumentParser(description=t("cli_description"))
    parser.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    parser.add_argument("--lang", default=None, metavar="LANG",
                        help=t("help_lang"))
    sub = parser.add_subparsers(dest="cmd")

    p_ui = sub.add_parser("ui", help=t("help_ui"))
    p_ui.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_ui.add_argument("--no-open", action="store_true", help=t("help_no_open"))

    p_list = sub.add_parser("list", help=t("help_list"))
    p_list.set_defaults(func=cmd_list)

    p_chk = sub.add_parser("check", help=t("help_check"))
    p_chk.add_argument("ids", nargs="*", metavar="ID")
    p_chk.set_defaults(func=cmd_check)

    p_rs = sub.add_parser("rescan", help=t("help_rescan"))
    p_rs.add_argument("ids", nargs="*", metavar="ID")
    p_rs.set_defaults(func=cmd_rescan)

    p_ins = sub.add_parser("install", help=t("help_install"))
    p_ins.add_argument("id", metavar="ID")
    p_ins.add_argument("--mode", choices=["merge", "clean"], default="merge",
                       help=t("help_mode"))
    p_ins.add_argument("--update-manifest", action="store_true",
                       help=t("help_update_manifest"))
    p_ins.add_argument("--allow-remote-host", action="append", default=[], metavar="HOST",
                       help=t("help_allow_remote_host"))
    p_ins.add_argument("--keep-zips", type=int, default=None, metavar="N",
                       help=t("help_keep_zips"))
    p_ins.set_defaults(func=cmd_install)

    p_un = sub.add_parser("uninstall", help=t("help_uninstall"))
    p_un.add_argument("id", metavar="ID")
    p_un.set_defaults(func=cmd_uninstall)

    p_add = sub.add_parser("add", help=t("help_add"))
    p_add.add_argument("url", metavar="URL")
    p_add.add_argument("--name", default=None, help=t("help_name"))
    p_add.set_defaults(func=cmd_add)

    p_rm = sub.add_parser("remove", help=t("help_remove"))
    p_rm.add_argument("id", metavar="ID")
    p_rm.add_argument("--purge", action="store_true", help=t("help_purge"))
    p_rm.set_defaults(func=cmd_remove)

    sub.add_parser("themes-dir", help=t("help_themes_dir"))

    args = parser.parse_args()
    _setup_console()
    if args.cmd is None or args.cmd == "ui":
        run_ui(port=getattr(args, "port", DEFAULT_PORT),
               no_open=getattr(args, "no_open", False))
        return EXIT_OK
    if args.cmd == "themes-dir":
        print(detect_themes_dir())
        return EXIT_OK
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
