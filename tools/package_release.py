#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包 Release 压缩包（Windows / macOS / Linux 三平台，纯代码最小集）。

用法（在项目根目录）:
  python tools/package_release.py [--version v0.1.0] [--platform windows|macos|linux|all]

产物写入 dist/：Typora-Themes-Manager-<版本>-<平台>.zip，解压后为单一顶层目录
Typora-Themes-Manager-<版本>/。压缩包刻意最小化——只含运行所需文件：
  通用: theme_manager.py / ui.html / README.md
  windows 另含: start-manager.bat（ANSI/GBK 编码，勿转存 UTF-8）
  macos、linux 另含: start-manager.command（zip 内已置 0755 可执行位，
  create_system=Unix 保证 macOS/Linux 解压工具恢复权限；Windows 资源管理器
  解压会丢失该位，属预期——Windows 用户使用 .bat）

AGENTS.md、repos.json、repos/ 等开发文档与运行数据一律不进包。
"""

import argparse
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "dist"

# 三平台共用的最小文件集（顺序即写入 zip 的顺序）；LICENSE 必须随发行版分发（MIT 条款）
COMMON_FILES = ["theme_manager.py", "ui.html", "README.md", "README.zh-CN.md", "LICENSE"]
# 各平台附加的启动脚本
PLATFORM_FILES = {
    "windows": ["start-manager.bat"],
    "macos": ["start-manager.command"],
    "linux": ["start-manager.command"],
}
# zip 内需要携带可执行位的文件（Unix 风格权限）
EXECUTABLE_FILES = {"start-manager.command"}


def build_zip(platform, version):
    files = COMMON_FILES + PLATFORM_FILES[platform]
    top_dir = "Typora-Themes-Manager-%s" % version
    out_path = OUT_DIR / ("Typora-Themes-Manager-%s-%s.zip" % (version, platform))
    missing = [f for f in files if not (ROOT / f).is_file()]
    if missing:
        raise SystemExit("缺少待打包文件: %s（请在项目根目录运行）" % ", ".join(missing))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel in files:
            src = ROOT / rel
            # 定死 ZipInfo：顶层目录统一 + mtime 取源文件（可复现）+ 显式权限位
            zi = zipfile.ZipInfo("%s/%s" % (top_dir, rel),
                                 date_time=time.localtime(src.stat().st_mtime)[:6])
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.create_system = 3  # Unix：使 external_attr 权限位对解压工具有效
            zi.external_attr = (0o755 if rel in EXECUTABLE_FILES else 0o644) << 16
            zf.writestr(zi, src.read_bytes())
    return out_path


def main():
    parser = argparse.ArgumentParser(description="打包三平台 Release 压缩包")
    parser.add_argument("--version", default="v0.1.0",
                        help="版本号（默认 v0.1.0；应与 git 标签一致）")
    parser.add_argument("--platform", default="all",
                        choices=["all"] + list(PLATFORM_FILES), help="目标平台（默认 all）")
    args = parser.parse_args()
    platforms = list(PLATFORM_FILES) if args.platform == "all" else [args.platform]
    for p in platforms:
        out = build_zip(p, args.version)
        names = zipfile.ZipFile(out).namelist()
        print("已生成 %s（%d 个文件, %.1f KB）" % (out.relative_to(ROOT), len(names),
                                              out.stat().st_size / 1024))
        for n in names:
            print("    " + n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
