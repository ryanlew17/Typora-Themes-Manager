#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Release 压缩包简化冒烟测试：模拟用户「下载 → 解压 → 首次运行」。

用法（在项目根目录，需先生成 dist/ 下的压缩包）:
  python tools/smoke_test_release.py [--version v0.1.0] [--keep]

对每个平台包依次执行:
  1. 沙盒解压 —— 解压到独立临时目录，检查单一顶层目录与文件结构
     （必需文件齐全，且无 .git / AGENTS.md / repos / repos.json / __pycache__ /
     .zcode / dist / tools 等开发与运行数据）
  2. zip 元数据 —— 条目名纯 ASCII（防平台解压器乱码）；macos/linux 包的
     start-manager.command 必须带 0755 权限位（Windows 解压不恢复权限属预期，
     验证的是包内位，macOS/Linux 解压工具据此恢复）；windows 包的
     start-manager.bat 必须为 ANSI/GBK + CRLF（cmd 解析要求）
  3. CLI 冷启动 —— 以隔离的 APPDATA/HOME 在解压副本上运行子进程，模拟新用户:
     --lang en-US --help / list（应报 0 repositories）/ zh-CN list（中文输出）；
     且 list 后不产生 repos.json（只读命令零写入）
  4. bash 语法检查（环境有 bash 时，对 macos/linux 包）: bash -n start-manager.command
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

EXPECTED = {  # 各平台包的必备文件（相对顶层目录）
    "common": ["theme_manager.py", "ui.html", "README.md", "README.zh-CN.md", "LICENSE"],
    "windows": ["start-manager.bat"],
    "macos": ["start-manager.command"],
    "linux": ["start-manager.command"],
}
FORBIDDEN = [".git", ".zcode", "__pycache__", "dist", "tools", "repos", "repos.json",
             "AGENTS.md", ".gitignore"]

failures = []


def check(cond, label, detail=""):
    status = "PASS" if cond else "FAIL"
    print("    [%s] %s%s" % (status, label, (" — " + detail) if detail else ""))
    if not cond:
        failures.append("%s %s" % (label, detail))


def sandbox_unzip(zip_path, sandbox):
    """模拟用户解压，返回解压出的顶层目录 Path（并断言只有一个顶层目录）。"""
    zf = zipfile.ZipFile(zip_path)
    zf.extractall(sandbox)
    tops = [p for p in sandbox.iterdir()]
    return tops[0] if len(tops) == 1 else None


def check_zip_meta(zip_path, platform):
    zf = zipfile.ZipFile(zip_path)
    infos = zf.infolist()
    check(all(all(ord(c) < 128 for c in zi.filename) for zi in infos),
          "zip 条目名纯 ASCII")
    if platform in ("macos", "linux"):
        zi = next(i for i in infos if i.filename.endswith("start-manager.command"))
        mode = (zi.external_attr >> 16) & 0o777
        check(mode == 0o755, "start-manager.command 带可执行位", "mode=%04o" % mode)
    else:
        zi = next(i for i in infos if i.filename.endswith("start-manager.bat"))
        raw = zf.read(zi)
        try:
            raw.decode("utf-8")
            is_gbk = False
        except UnicodeDecodeError:
            is_gbk = True
        check(is_gbk and b"\r\n" in raw, "start-manager.bat 为 ANSI/GBK + CRLF")


def run_cli(top_dir, sandbox, args, expect_substr=""):
    """在隔离 HOME/APPDATA 下对解压副本跑 CLI，返回 (退出码, 输出)。"""
    env = dict(os.environ)
    env["APPDATA"] = str(sandbox / "appdata")   # Windows: 主题目录探测隔离
    env["HOME"] = str(sandbox / "home")          # macOS/Linux
    env["TTM_LANG"] = ""                          # 不残留外部语言偏好
    proc = subprocess.run([sys.executable, "theme_manager.py"] + args,
                          cwd=top_dir, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    out = (proc.stdout or "") + (proc.stderr or "")
    if expect_substr:
        check(expect_substr in out, "输出包含 %r" % expect_substr,
              out.strip().splitlines()[-1] if out.strip() else "(无输出)")
    return proc.returncode, out


def test_package(zip_path, platform, version, keep):
    print("== %s ==" % zip_path.name)
    sandbox = Path(tempfile.mkdtemp(prefix="ttm-smoke-%s-" % platform))
    try:
        top = sandbox_unzip(zip_path, sandbox)
        check(top is not None and top.is_dir() and top.name ==
              "Typora-Themes-Manager-%s" % version, "单一顶层目录",
              top.name if top else "解压得到 %d 个顶层条目" % len(list(sandbox.iterdir())))
        present = {str(p.relative_to(top)).replace("\\", "/") for p in top.rglob("*")}
        for f in EXPECTED["common"] + EXPECTED[platform]:
            check((top / f).is_file(), "包含 %s" % f)
        extra = sorted(present - set(EXPECTED["common"] + EXPECTED[platform]))
        check(not extra, "无多余文件", ", ".join(extra[:8]))
        for bad in FORBIDDEN:
            check(bad not in present and not (top / bad).exists(), "不含 %s" % bad)

        check_zip_meta(zip_path, platform)

        # CLI 冷启动（新用户首次运行）
        rc, _ = run_cli(top, sandbox, ["--lang", "en-US", "--help"])
        check(rc == 0, "--lang en-US --help 退出码 0")
        rc, _ = run_cli(top, sandbox, ["--lang", "en-US", "list"],
                        expect_substr="0 repositories")
        check(rc == 0, "--lang en-US list 退出码 0")
        rc, _ = run_cli(top, sandbox, ["--lang", "zh-CN", "list"],
                        expect_substr="已注册 0 个仓库")
        check(rc == 0, "--lang zh-CN list 退出码 0")
        check(not (top / "repos.json").exists(), "list 只读，未生成 repos.json")

        # bash 语法检查（macos/linux 包；环境有 bash 才执行）
        if platform in ("macos", "linux"):
            bash = shutil.which("bash")
            if bash:
                proc = subprocess.run([bash, "-n", str(top / "start-manager.command")],
                                      capture_output=True, text=True)
                check(proc.returncode == 0, "bash -n start-manager.command 语法通过")
            else:
                print("    [SKIP] 环境无 bash，跳过 .command 语法检查")
    finally:
        if keep:
            print("    沙盒保留: %s" % sandbox)
        else:
            shutil.rmtree(sandbox, ignore_errors=True)
    print()


def main():
    for stream in (sys.stdout, sys.stderr):  # Windows 控制台中文输出兜底
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Release 压缩包冒烟测试")
    parser.add_argument("--version", default="v0.1.0")
    parser.add_argument("--keep", action="store_true", help="保留解压沙盒目录")
    args = parser.parse_args()

    zips = sorted(glob.glob(str(DIST / ("Typora-Themes-Manager-%s-*.zip" % args.version))))
    if not zips:
        print("dist/ 下没有版本 %s 的压缩包，先运行: python tools/package_release.py"
              % args.version)
        return 1
    for z in zips:
        platform = Path(z).stem.rsplit("-", 1)[-1]
        test_package(Path(z), platform, args.version, args.keep)

    n = len(zips)
    if failures:
        print("结果: ✗ %d 项失败（共 %d 个包）" % (len(failures), n))
        for f in failures:
            print("  - " + f)
        return 1
    print("结果: ✓ 全部通过（%d 个包）" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
