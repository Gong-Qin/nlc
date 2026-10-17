# -*- coding: utf-8 -*-
"""NLC Windows 一键安装（便携安装的 Windows 版）。

「便携」在 Windows 上同样是四条具体含义：
    1. **不污染系统** —— 不写注册表、不改 PATH、不装任何第三方包
    2. **装在一个目录里** —— 默认 `%LOCALAPPDATA%\\Programs\\nlc`，想删整体删掉
    3. **离线可装** —— 代码零第三方依赖，不需要联网
    4. **解压即用** —— 不想装就直接双击包里的 `nlc.cmd`

对照 Linux 版的 `install.sh`，步骤一一对应，但有几处**必须不一样**：
    - 不写 XDG 路径，改写 `%LOCALAPPDATA%`（Windows 上没人认 ~/.local）
    - 「桌面图标」改成「开始菜单项」（Windows 的开始菜单才是找程序的地方）
    - 清单文件名前面加点（`.nlc-install-manifest`），Windows 上默认隐藏

⚠️ 本脚本是**会往磁盘写东西的安装脚本**，所以每一处写入都先做安全检查，
   并且把"创建了什么"逐条记进清单 —— 卸载只认清单，绝不靠猜。

用法：
    python install_windows.py                装到默认位置
    python install_windows.py --prefix D:\\nlc  装到别处
    python install_windows.py --dry-run      只报告要做什么，不动磁盘
    python install_windows.py --force        覆盖已存在的安装
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List

# 英文 Windows 的控制台默认 cp1252，打印中文会 UnicodeEncodeError —— 统一强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
SRC = HERE.parent                      # 项目根目录（nlc/ 的上一层）

#: 不往安装目录里复制的东西
SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode", "dist", "_cfgtest",
             "_gui_smoke_cfg"}
SKIP_SUFFIX = (".pyc", ".pyo", ".bak")

MANIFEST_NAME = ".nlc-install-manifest"


# --------------------------------------------------------------------------- #
# 前置检查
# --------------------------------------------------------------------------- #
def check_python() -> List[str]:
    problems: List[str] = []
    if sys.version_info < (3, 8):
        problems.append("Python 版本太低：%d.%d（需要 3.8 以上）" % sys.version_info[:2])
    try:
        import tkinter                        # noqa: F401
    except ImportError:
        problems.append("这套 Python 没有带 tkinter —— 图形界面起不来。"
                        "命令行版（nlc tr / risk / mcp）仍可正常使用。"
                        "建议换用 python.org 的官方安装包。")
    return problems


def check_target(path: Path) -> List[str]:
    """★ 安装前的安全阀：绝不往这些地方装。"""
    problems: List[str] = []
    raw = str(path).rstrip("\\/")
    home = Path.home()

    if not raw:
        problems.append("目标目录是空的")
        return problems
    # 1) 盘符根：C:\  /  D:\
    if len(raw) <= 3 and raw[1:2] == ":":
        problems.append("拒绝装到盘符根目录：%s" % raw)
    # 2) 用户主目录本身
    if Path(raw) == home:
        problems.append("拒绝装到用户主目录本身：%s" % raw)
    # 3) Windows / Program Files 这些系统位置
    low = raw.lower()
    for bad in ("\\windows", "\\program files", "\\programdata", "\\users\\public"):
        if low.endswith(bad):
            problems.append("拒绝装到系统目录：%s" % raw)
    # 4) 路径至少两层（C:\nlc 是最浅的合理值）
    if len(Path(raw).parts) < 2:
        problems.append("路径太浅，至少要两层：%s" % raw)
    return problems


def default_target() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / "nlc"


def start_menu_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


# --------------------------------------------------------------------------- #
# 复制代码
# --------------------------------------------------------------------------- #
def copy_code(src: Path, dst: Path) -> List[str]:
    """复制代码，返回复制过去的相对路径列表。"""
    copied: List[str] = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = Path(root).relative_to(src)
        # tests/ 一起带上：别人能自己跑自测，这是"可验证"的一部分
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.endswith(SKIP_SUFFIX) or f == MANIFEST_NAME:
                continue
            # 「安装包/dist」里的大文件不复制（自解压包本身没必要装进去）
            if rel.parts[:1] == ("dist",):
                continue
            shutil.copy2(Path(root) / f, dst / rel / f)
            copied.append(str(rel / f))
    return copied


LAUNCHER = r"""@echo off
rem NLC launcher written by install_windows.py -- do not edit by hand.
rem
rem NOTE: keep this file ASCII-only. cmd.exe reads batch files in the OEM code
rem page, so non-ASCII text here gets garbled -- and a garbled "rem" line can
rem even end up being executed as a command.
setlocal EnableExtensions
chcp 65001 >nul 2>nul
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

rem --- CRITICAL: run from NLC's own directory -------------------------------
rem `python -m nlc` can only find the package when the current directory IS the
rem one containing the nlc\ package. Without this pushd, launching from anywhere
rem else (a Start Menu entry, a shortcut, another folder) dies with
rem "No module named nlc". This was a real bug: the dev nlc.cmd had the pushd,
rem the installed one lost it, so only in-place launches happened to work.
pushd "%HERE%" >nul 2>nul

if "%~1"=="" (
    "{PYEXE}" -m nlc gui
) else (
    "{PYEXE}" -m nlc %*
)
set "RC=%ERRORLEVEL%"
popd >nul 2>nul
exit /b %RC%
"""


def write_launcher(target: Path, pyexe: str) -> Path:
    """写出安装目录里的启动器。

    ⚠️ 启动器里嵌了 **Python 的完整路径**。如果装到含中文的目录、
       或者用的是中文路径的 Python（例如某些 uv/conda 安装），
       写出来的 .cmd 就会含非 ASCII 字符 —— cmd.exe 读它时正文乱码。
    所以这里**逐字符校验并给出可执行的建议**，而不是等用户双击时才发现。
    """
    bad = sorted({c for c in ("%s%s" % (pyexe, target)) if ord(c) > 127})
    if bad:
        raise ValueError(
            "拒绝写启动器：路径里有非 ASCII 字符 %s。\n"
            "       .cmd 只能可靠地承载 ASCII，写进去会在 cmd.exe 里变乱码。\n"
            "       解决办法（任选一个）：\n"
            "         · 用纯 ASCII 的 Python：%s -m nlc install --force 会自动用当前解释器，\n"
            "           你可以先确认它是不是中文路径；\n"
            "         · 装到纯 ASCII 目录：--prefix C:/nlc\n"
            "       代码本身仍可正常用，只是少了这个双击入口。" % (bad, sys.executable))
    p = target / "nlc.cmd"
    p.write_bytes(LAUNCHER.replace("{PYEXE}", pyexe).encode("ascii"))
    return p


def write_start_menu(target: Path, launcher: Path) -> Path | None:
    """在开始菜单里放一个入口。

    Windows 上「开始菜单」才是找程序的地方 —— 相当于 Linux 版的桌面图标。
    这里放一个 .cmd 而不是 .lnk：不需要走 COM，也不依赖任何额外工具，
    而且用户能在开始菜单里直接搜到「NLC」。

    ⚠️ **文件名带中文是故意的**（用户能搜到「NLC 自然语言命令行」），
      但**文件内容必须能被 ASCII 编出来**：
      cmd.exe 按 OEM 代码页读批处理，`.cmd` 里出现中文正文会乱码，
      乱码的 `rem` 行甚至会被当成命令执行。
      所以正文一律英文，中文只允许出现在**文件名**里 —— 那是 NTFS 的事，与代码页无关。

    ⚠️ 还有一个更隐蔽的：**被 `call` 的那个路径本身不能含中文**。
      本项目的默认安装目录是纯 ASCII（%LOCALAPPDATA%\\Programs\\nlc），
      所以成立；但用 `--prefix` 装到中文目录时就会炸。
      因此这里**逐个字符校验**，含中文就明确报错并给建议，
      而不是让用户拿到一个报 `UnicodeEncodeError` 的安装。
    """
    folder = start_menu_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    # 前置校验：目标启动器路径必须能写进 ASCII 批处理
    non_ascii = sorted({c for c in str(launcher) if ord(c) > 127})
    if non_ascii:
        raise ValueError(
            "拒绝创建开始菜单入口：启动器路径里有非 ASCII 字符 %s，"
            "写进 .cmd 会被 cmd.exe 按 OEM 代码页读成乱码。\n"
            "       请改用纯 ASCII 的安装目录，例如 --prefix "
            "%%LOCALAPPDATA%%\\Programs\\nlc（默认值本来就是纯 ASCII）。" % non_ascii)

    entry = folder / "NLC 自然语言命令行.cmd"
    # ★ 下面三行里的中文路径名是**绝对不能出现的** ——
    #   连注释里写「见 07_某某.md」都会让整个 .encode("ascii") 炸掉。
    #   这就是为什么这里的注释也必须全英文。
    entry.write_bytes(
        ("@echo off\r\n"
         "rem Created by the NLC installer; removed on uninstall.\r\n"
         "rem Kept ASCII-only on purpose -- see the docs folder, section 07.\r\n"
         'call "%s" %%*\r\n' % launcher).encode("ascii"))
    return entry


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NLC Windows 一键安装")
    ap.add_argument("--prefix", default="", help="安装到哪个目录")
    ap.add_argument("--dry-run", action="store_true", help="只报告要做什么")
    ap.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    ap.add_argument("--no-shortcut", action="store_true", help="不建开始菜单项")
    args = ap.parse_args(argv)

    target = Path(args.prefix).expanduser().resolve() if args.prefix else default_target()

    print("=" * 66)
    print("NLC · 自然语言命令行 —— Windows 安装")
    print("=" * 66)
    print("  源码目录：%s" % SRC)
    print("  安装位置：%s" % target)
    print("  使用的 Python：%s" % sys.executable)
    print("  版本：%d.%d.%d" % sys.version_info[:3])
    print()

    print("[1/6] 环境自检")
    problems = check_python()
    for p in problems:
        print("      ! %s" % p)
    if any("版本太低" in p for p in problems):
        print("\n安装中止：Python 版本不达标。")
        return 2
    if not problems:
        print("      Python 版本、tkinter 都正常")

    print("[2/6] 目标位置检查")
    bad = check_target(target)
    for b in bad:
        print("      ✕ %s" % b)
    if bad:
        print("\n安装中止：目标位置不安全。")
        return 2
    if target.exists() and any(target.iterdir()) and not args.force:
        print("      目标目录已有内容：%s" % target)
        print("      要覆盖请加 --force；想换位置请加 --prefix")
        return 2
    print("      位置检查通过")

    if args.dry_run:
        print("\n--dry-run：以上是计划，未做任何改动。")
        return 0

    print("[3/6] 复制代码")
    target.mkdir(parents=True, exist_ok=True)
    copied = copy_code(SRC, target)
    print("      共 %d 个文件 → %s" % (len(copied), target))

    print("[4/6] 生成启动器")
    launcher = write_launcher(target, sys.executable)
    print("      %s" % launcher)

    print("[5/6] 建开始菜单入口")
    entry = None
    if args.no_shortcut:
        print("      按参数跳过")
    else:
        entry = write_start_menu(target, launcher)
        print("      %s" % entry if entry else "      （建不了，跳过；可直接用安装目录里的 nlc.cmd）")

    print("[6/6] 写安装清单")
    manifest = {
        "name": "nlc",
        "version": "0.2.0-windows",
        "installed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "platform": sys.platform,
        "python": sys.executable,
        "target": str(target),
        "launcher": str(launcher),
        "start_menu_entry": str(entry) if entry else "",
        "files": copied,
        "note": "卸载只删这份清单里记着的东西，不扫、不猜。",
    }
    mp = target / MANIFEST_NAME
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("      %s" % mp)

    print()
    print("装好了。三种用法：")
    print("  ① 双击这个文件打开图形界面：")
    print("     %s" % launcher)
    print("  ② 开始菜单里搜「NLC」")
    print('  ③ 命令行： "%s" -m nlc tr "看磁盘占用"' % sys.executable)
    print()
    print("接下来要做的第一件事：填一个 AI 通道的密钥")
    print('      "%s" -m nlc set-key deepseek sk-你的密钥' % sys.executable)
    print()
    print("卸载：%s" % (Path(__file__).with_name("uninstall_windows.py")))
    print("      或直接双击包里的「卸载 NLC.cmd」")
    return 0


if __name__ == "__main__":
    sys.exit(main())
