#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 NLC 打成可分发的安装包。

为什么用 Python 而不是纯 shell 来做：
    **在 Windows 上打 tar 包会丢掉可执行位。** 用 tarfile 显式写 mode，
    不管在哪台机器上打，出来的包权限都一样对。

产出（默认落在 packaging/dist/）：
    nlc-<版本>.tar.gz          普通压缩包
    nlc-<版本>.run             自解压单文件（sh 头 + tar.gz），sh 它就能装
    SHA256SUMS                 校验和

用法：
    python3 packaging/build_package.py                    # Linux：tar.gz + .run
    python3 packaging/build_package.py --windows          # Windows：.zip
    python3 packaging/build_package.py --out /tmp/dist

★ Windows 模式（--windows）是**另加的一条分支**，刻意**没有改动上面任何一行 Linux 逻辑** ——
  理由见《09_待办与决策.md》§2.5：为了 Windows 去动 Linux 的打包逻辑只会引入风险。
  两者唯一的共同点是要跳过 __pycache__。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "packaging" / "dist"

#: 打进包里的顶层条目（相对仓库根）
INCLUDE = ["nlc.sh", "README.md", ".gitignore", "nlc", "tests", "packaging/nlc.svg"]
#: 发布包根目录下还额外放这两个（从 packaging/ 提上来，方便直接 sh install.sh）
PROMOTE = ["packaging/install.sh", "packaging/uninstall.sh"]

SKIP_DIRS = {"__pycache__", ".git", "dist", ".tmpcfg", "node_modules"}
SKIP_PATTERNS = [r".*\.pyc$", r".*\.pyo$", r"_proto_.*\.json$", r".*\.tgz$", r".*\.run$",
                 r".*\.tar\.gz$", r"\.DS_Store$"]


def version() -> str:
    init = (ROOT / "nlc" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    return m.group(1) if m else "0.0.0"


def _skip(name: str) -> bool:
    return any(re.match(p, name) for p in SKIP_PATTERNS)


def collect() -> list:
    """返回 [(归档内路径, 绝对路径, 是否可执行)]。"""
    items = []
    for rel in INCLUDE:
        src = ROOT / rel
        if not src.exists():
            print("  ! 缺少 %s（跳过）" % rel, file=sys.stderr)
            continue
        if src.is_file():
            items.append((rel, src, _is_exec(rel)))
            continue
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in sorted(filenames):
                if _skip(fn):
                    continue
                full = Path(dirpath) / fn
                arc = str(full.relative_to(ROOT)).replace(os.sep, "/")
                items.append((arc, full, _is_exec(arc)))
    for rel in PROMOTE:
        src = ROOT / rel
        if src.exists():
            items.append((Path(rel).name, src, True))     # 提到根目录
    return items


def _is_exec(arc: str) -> bool:
    return arc.endswith(".sh")


def build_tarball(dest: Path, top: str, items: list) -> None:
    """写 tar.gz，显式设置权限位。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz", format=tarfile.GNU_FORMAT) as tf:
        # 顶层目录条目
        ti = tarfile.TarInfo(top)
        ti.type = tarfile.DIRTYPE
        ti.mode = 0o755
        tf.addfile(ti)

        seen_dirs = {top}
        for arc, src, is_exec in items:
            # 补目录条目
            parts = arc.split("/")
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                full_d = "%s/%s" % (top, d)
                if full_d not in seen_dirs:
                    seen_dirs.add(full_d)
                    di = tarfile.TarInfo(full_d)
                    di.type = tarfile.DIRTYPE
                    di.mode = 0o755
                    tf.addfile(di)
            data = normalize_bytes(src, arc)
            ti = tarfile.TarInfo("%s/%s" % (top, arc))
            ti.size = len(data)
            ti.mode = 0o755 if is_exec else 0o644
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mtime = 0                              # 可复现构建
            tf.addfile(ti, io.BytesIO(data))


#: 需要在打包时把换行统一成 LF 的文本类型。
#: ★ 为什么必须做：在 Windows 上写的文件是 CRLF，Linux 的 dash 会把 `set -e\r`
#:   读成 `set -e` 后面跟一个回车字符，直接报 `set: Illegal option -`。
#:   这个错**只有在 Linux 上才会出现**，在 Windows 的 Git Bash 里 `sh -n` 是过的 ——
#:   所以必须在打包环节统一处理，而不是指望开发机的编辑器设置。
TEXT_EXTS = {".sh", ".py", ".md", ".svg", ".json", ".txt", ".conf", ".desktop",
             ".gitignore", ".cfg", ".toml", ".yaml", ".yml"}


def _is_text(arc: str) -> bool:
    name = arc.split("/")[-1]
    if name in (".gitignore",):
        return True
    return os.path.splitext(arc)[1].lower() in TEXT_EXTS


def normalize_bytes(src: Path, arc: str) -> bytes:
    """读进内存并把 CRLF 统一成 LF（只对文本文件）。"""
    data = src.read_bytes()
    if _is_text(arc):
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data


RUN_HEADER = """#!/bin/sh
# NLC 自解压安装包（版本 %(ver)s）
#
# 用法：
#   sh %(name)s              # 解压并安装
#   sh %(name)s --prefix DIR # 装到指定位置
#   sh %(name)s --dry-run    # 只报告，不落盘
#
# 说明：NLC 零第三方依赖，包里已有全部代码，安装过程不联网、不动系统目录。
set -e
SKIP=$(awk '/^__NLC_ARCHIVE_BELOW__$/ {print NR + 1; exit}' "$0")
TMP=$(mktemp -d "${TMPDIR:-/tmp}/nlc.XXXXXX")
trap 'rm -rf "$TMP"' EXIT INT TERM
tail -n +"$SKIP" "$0" > "$TMP/pkg.tar.gz"
mkdir -p "$TMP/x"
tar xzf "$TMP/pkg.tar.gz" -C "$TMP/x"
cd "$TMP/x/%(top)s"
echo "已解压到临时目录，开始安装…"
sh install.sh "$@"
echo
echo "安装脚本已跑完（临时目录会在退出时自动清理）"
exit 0
__NLC_ARCHIVE_BELOW__
"""


def build_run(dest: Path, ver: str, top: str, tarball: Path) -> None:
    header = RUN_HEADER % {"ver": ver, "name": dest.name, "top": top}
    with open(dest, "wb") as out:
        out.write(header.encode("utf-8"))
        out.write(tarball.read_bytes())
    os.chmod(dest, 0o755)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


# =========================================================================== #
# Windows 模式：产出 .zip（解压即用）
# =========================================================================== #
#: Windows 包里要带什么。cmd 启动器是**提到根目录**的（用 PROMOTE_WIN），
#: 其余与 Linux 包保持一致的收录范围。
INCLUDE_WIN = ["README.md", "nlc", "tests", "packaging/nlc.svg",
               "packaging/install_windows.py", "packaging/uninstall_windows.py"]
#: 提到包根目录的三个 .cmd —— 用户解压后第一眼看到的就是它们
PROMOTE_WIN = ["nlc.cmd", "install.cmd", "uninstall.cmd"]

#: ★ 2026-10-03 新增：解压后 zip 里会有一层 `nlc-<版本>-windows\` 目录。
#:   用户面对的是"一个文件夹"，双击它才看到 nlc.cmd —— 很多人会在这里卡住，
#:   或者干脆双击 zip 本身，然后报"找不到相应的文件夹"。
#:   所以在**包的最外层**再放一个中文名启动器，进去就双击它，不用找。
#:   （它内部按相对路径转调，所以放哪一层都能工作。）
TOP_LAUNCHER_ZH = "启动NLC.cmd"

#: 这个启动器的正文。**必须纯 ASCII**（理由同 nlc.cmd）。
#:   %~dp0 是"本文件所在目录"，所以它会自动转调同目录下一层的 nlc.cmd。
LAUNCHER_ZH_BODY = b"""@echo off
rem ===========================================================================
rem  NLC - Natural Language Command-line
rem
rem  Double-click to start. No arguments needed.
rem
rem  This file lives at the TOP of the zip.  The real launcher is one folder
rem  down (nlc-<version>\\nlc.cmd).  This wrapper just forwards to it, so you
rem  never have to hunt for the right folder after unzipping.
rem
rem  NOTE: keep this file pure ASCII.  cmd.exe reads batch files in the OEM
rem  code page; a non-ASCII char (even in a comment) turns into garbage, and
rem  a garbage "rem" line gets executed as a command.
rem ===========================================================================
setlocal
set "HERE=%~dp0"
if "%HERE:~-1%"=="\\" set "HERE=%HERE:~0,-1%"

rem 1) the installed copy, if there is one
if exist "%LOCALAPPDATA%\\Programs\\nlc\\nlc.cmd" (
    call "%LOCALAPPDATA%\\Programs\\nlc\\nlc.cmd" %*
    set "RC=%ERRORLEVEL%"
    endlocal & exit /b %RC%
)

rem 2) the folder that came out of the zip
for /d %%D in ("%HERE%*") do (
    if exist "%%~fD\\nlc.cmd" (
        call "%%~fD\\nlc.cmd" %*
        set "RC=%ERRORLEVEL%"
        endlocal & exit /b %RC%
    )
)

rem 3) same folder as this file (already-unzipped layout)
if exist "%HERE%nlc.cmd" (
    call "%HERE%nlc.cmd" %*
    set "RC=%ERRORLEVEL%"
    endlocal & exit /b %RC%
)

echo.
echo   Cannot find NLC.cmd.  Looked in:
echo     - %%LOCALAPPDATA%%\\Programs\\nlc\\
echo     - every folder next to this file
echo     - this same folder
echo.
echo   If you just unzipped the package, open the folder inside it and
echo   double-click nlc.cmd.
echo.
pause
exit /b 1
"""

#: ★ 与 Linux 包的一个**真实差异**：.cmd 必须存成 CRLF。
#:   Linux 包要求文本全 LF（见 TEXT_EXTS 处的说明），Windows 正好相反 ——
#:   批处理是 Windows 自己的文本格式，CRLF 才是原生形态。
#:   （实测：LF 的 .cmd 在 Windows 11 上 `goto` 与多行括号块也能跑，
#:     但没必要去赌 —— 发出去的包就按目标平台的原生格式走。）
WIN_CRLF_EXTS = {".cmd", ".bat"}


def collect_win() -> list:
    """Windows 包的收录清单。返回 [(包内路径, 绝对路径)]。"""
    items = []
    for rel in INCLUDE_WIN:
        src = ROOT / rel
        if not src.exists():
            print("  ! 缺少 %s（跳过）" % rel, file=sys.stderr)
            continue
        if src.is_file():
            items.append((rel, src))
            continue
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in sorted(filenames):
                if _skip(fn):
                    continue
                full = Path(dirpath) / fn
                arc = str(full.relative_to(ROOT)).replace(os.sep, "/")
                items.append((arc, full))
    for rel in PROMOTE_WIN:
        src = ROOT / rel
        if not src.exists():
            print("  ! 缺少 %s（跳过）" % rel, file=sys.stderr)
            continue
        items.append((rel, src))          # 本来就在根目录，不需要提升
    return items


def _win_bytes(src: Path, arc: str) -> bytes:
    """读进内存，.cmd/.bat 统一成 CRLF，其余原样（Python 源码不动）。"""
    data = src.read_bytes()
    if os.path.splitext(arc)[1].lower() in WIN_CRLF_EXTS:
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")
    return data


def build_zip(dest: Path, top: str, items: list) -> None:
    """写 .zip。固定时间戳 ⇒ 同样的源码打出同样的包（可复现构建）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, src in items:
            # ★ mtime 固定成 1980-01-01：zip 格式的日期下限，
            #   不固定的话每次打包字节都不同，没法比对校验和。
            info = zipfile.ZipInfo("%s/%s" % (top, arc), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            # 0o644 对应 Windows 上的普通文件；显式写上，免得解出来权限乱掉
            info.external_attr = 0o644 << 16
            zf.writestr(info, _win_bytes(src, arc))
        # ★ 顶层启动器：放在 `<top>/` 里，**不在** `nlc-<版本>-windows/` 那一层。
        #   这样用户解压后第一眼就看到中文名入口，不用先进去再找。
        info = zipfile.ZipInfo("%s/%s" % (top, TOP_LAUNCHER_ZH), date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        zf.writestr(info, LAUNCHER_ZH_BODY.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))


README_ZIP = """NLC 自然语言命令行 —— Windows 便携版 %(ver)s
================================================================

## 第一步：解压，然后双击「启动NLC.cmd」

★ 解压后会看到一个文件夹  nlc-%(ver)s-windows\\
  （zip 里带一层目录是正常的，不然解压出来的东西会散一地）

  双击包里的  启动NLC.cmd  → 图形界面就开出来了。
  它会自动找到下面那一层的 nlc.cmd，**你不用自己找**。

  ▶ 别直接双击 zip 文件本身 —— 那样打不开。
  ▶ 报「找不到相应的文件夹」？说明你少了中间那层：
    先打开  nlc-%(ver)s-windows\\  这个文件夹，
    里面的  nlc.cmd  才是真正的主程序。

## 怎么用（三选一，都不需要管理员权限）

  1) 免安装，直接用
       双击    启动NLC.cmd        → 打开图形界面（推荐，最省事）
       或双击  nlc-%(ver)s-windows\\nlc.cmd
       （把整个文件夹放到哪都能用，别放在需要管理员权限的目录里）

  2) 装到本机（有开始菜单入口）
       进入  nlc-%(ver)s-windows\\  文件夹，双击  install.cmd
       装到      %%LOCALAPPDATA%%\\Programs\\nlc
       之后可以从开始菜单搜「NLC 自然语言命令行」
       卸载      双击 uninstall.cmd

  3) 命令行
       nlc.cmd tr "看看谁占着 8080 端口"
       nlc.cmd risk "del /f /s /q C:\\Windows"
       nlc.cmd doctor          ← 出问题时先跑这条，一次报全

## 第一次用之前：填一个 API Key

  翻译功能需要一个大模型；**切词块、判风险、查词库、离线解释都不需要**。

       nlc.cmd set-key deepseek sk-你的密钥
       nlc.cmd test deepseek          ← 体检一下通不通

  密钥存在 %%APPDATA%%\\nlc\\secrets.json，不在本包里、不在代码里。

## 需要什么

  Windows 8.1 或更高 + Python 3.8 或更高（https://www.python.org/downloads/windows/，
  安装时勾上「Add python.exe to PATH」）。
  官方安装包**自带 tkinter**，所以图形界面不需要额外装任何东西。
  NLC 只用 Python 标准库，**零第三方依赖**。

  ▶ 装了好几个 Python？不用管。
    启动NLC.cmd 会**逐个试**，挑第一个真能 import tkinter 的那个。
    （有些精简版 Python 没编译 tkinter，挑错了界面就起不来。）

## 两条安全设计（请先看懂再用）

  1. AI 只翻译、不执行。它不会自动运行任何命令，命令交给你自己判断、自己按。
  2. 风险判定由**本地规则引擎**做，不是让大模型说"这条很安全"。
     关键路径的删除（例如 del /f /s /q C:\\Windows）会被标成「已禁止」，点不动。

## 目录里有什么

  nlc\\        代码（纯标准库）
  tests\\      四条自测，想自己验证就跑：python tests\\selftest.py 等
  packaging\\  安装 / 卸载脚本（装过之后手里就有它们）
  真机截图\\    界面的真实截图

校验和见同目录的 SHA256SUMS。
"""


def build_windows(out: Path) -> int:
    ver = version()
    top = "nlc-%s-windows" % ver
    out.mkdir(parents=True, exist_ok=True)

    print("═══ 打包 NLC %s（Windows 便携版）═══\n" % ver)
    items = collect_win()
    print("  收录 %d 个文件" % len(items))

    # 包里带一份《怎么用》，免得解压的人不知道从哪下手
    readme = out / "_README_用前必读.txt"
    readme.write_text(README_ZIP % {"ver": ver}, encoding="utf-8")
    items.append(("用前必读.txt", readme))

    zpath = out / ("%s.zip" % top)
    build_zip(zpath, top, items)
    size_kb = zpath.stat().st_size / 1024
    print("  ✔ %s  (%.1f KB，解压即用)" % (zpath.name, size_kb))

    sums = out / "SHA256SUMS"
    sums.write_text("%s  %s\n" % (sha256(zpath), zpath.name), encoding="utf-8")
    print("  ✔ %s" % sums.name)

    # ── 自检：.cmd 必须是 CRLF（Windows 批处理的原生格式）────────────
    print("\n  换行符检查（.cmd 必须是 CRLF）：")
    bad_crlf = []
    with zipfile.ZipFile(zpath) as zf:
        for name in zf.namelist():
            if not name.lower().endswith((".cmd", ".bat")):
                continue
            data = zf.read(name)
            if b"\n" in data.replace(b"\r\n", b""):
                bad_crlf.append(name)
    if bad_crlf:
        print("    ✘ 仍是 LF：%s" % "、".join(bad_crlf))
    else:
        print("    全部是 CRLF ✔")

    # ── 自检：包里不能有 __pycache__ / .pyc ─────────────────────────
    print("\n  垃圾文件检查：")
    with zipfile.ZipFile(zpath) as zf:
        junk = [n for n in zf.namelist()
                if "__pycache__" in n or n.endswith((".pyc", ".pyo"))]
    print("    %s" % ("干净 ✔" if not junk else "✘ 混进了 %d 个：%s"
                     % (len(junk), "、".join(junk[:5]))))

    # ── 自检：包能不能真的解开并跑起来 ────────────────────────────
    print("\n  解压冒烟（在临时目录里真跑一次）：")
    stage = out / "_pkgtest_win"
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    with zipfile.ZipFile(zpath) as zf:
        zf.extractall(stage)
    root = stage / top
    rc = 0
    try:
        import subprocess as sp

        # ① 顶层启动器必须在，而且正文是纯 ASCII（中文名文件可以有中文，
        #    正文不行 —— 乱码的 rem 行会被当命令执行）
        top_launcher = root / TOP_LAUNCHER_ZH
        if not top_launcher.exists():
            print("    ✘ 顶层缺少 %s（用户解压后会不知道从哪下手）" % TOP_LAUNCHER_ZH)
            rc = 1
        else:
            body = top_launcher.read_bytes()
            if any(b > 0x7F for b in body):
                print("    ✘ %s 正文含非 ASCII 字符" % TOP_LAUNCHER_ZH)
                rc = 1
            else:
                print("    ✔ 顶层有 %s（纯 ASCII）" % TOP_LAUNCHER_ZH)

        # ② 直接用 Python 跑三条命令
        for args in (["-m", "nlc", "--version"],
                     ["-m", "nlc", "risk", "del /f /s /q C:\\Windows"],
                     ["-m", "nlc", "doctor", "--no-tests"]):
            p = sp.run([sys.executable] + args, cwd=str(root),
                       capture_output=True, timeout=120)
            okmark = "✔" if p.returncode == 0 else "✘"
            print("    %s python %s" % (okmark, " ".join(args)))
            if p.returncode != 0:
                rc = 1
                print("      %s" % (p.stderr or b"").decode("utf-8", "replace")[:200])

        # ③ ★ 经由 nlc.cmd 跑 —— 这条才抓得到"选错 Python"这类 bug。
        #   2026-10-03 就是靠它发现 nlc.cmd 会挑到没有 tkinter 的 Python。
        #   只用 sys.executable 测是**测不出来的**，因为那绕过了启动器。
        nlc_cmd = root / "nlc.cmd"
        if nlc_cmd.exists():
            p = sp.run(["cmd", "/c", "nlc.cmd", "--version"], cwd=str(root),
                       capture_output=True, timeout=120)
            out_txt = (p.stdout or b"").decode("utf-8", "replace")
            if p.returncode != 0:
                print("    ✘ nlc.cmd --version 失败（退出码 %s）" % p.returncode)
                rc = 1
            else:
                print("    ✔ nlc.cmd --version → %s" % out_txt.strip().splitlines()[0])
            # 挑中的 Python 必须能 import tkinter，否则图形界面起不来
            p = sp.run(["cmd", "/c", "nlc.cmd", "doctor", "--no-tests"], cwd=str(root),
                       capture_output=True, timeout=120)
            doc = (p.stdout or b"").decode("utf-8", "replace")
            tk_line = next((ln.strip() for ln in doc.splitlines()
                            if "tkinter" in ln or "Tk " in ln), "")
            if "没有" in tk_line:
                print("    ✘ nlc.cmd 挑到的 Python 没有 tkinter —— 图形界面会起不来")
                print("      %s" % tk_line)
                rc = 1
            elif tk_line:
                print("    ✔ nlc.cmd 挑的 Python 有 tkinter：%s" % tk_line)
            else:
                print("    ! doctor 输出里没找到 tkinter 那行（跳过该项判定）")
        else:
            print("    ✘ 包里没有 nlc.cmd")
            rc = 1
    except Exception as exc:                                       # noqa: BLE001
        print("    ✘ 冒烟跑不起来：%s" % exc)
        rc = 1
    shutil.rmtree(stage, ignore_errors=True)      # 用完即删（规则 2）

    # ── 顺带产出一份 SHA256 供发布时贴 ─────────────────────────────
    print("\n  下一步：")
    print("    本机用：解压后双击 %s" % TOP_LAUNCHER_ZH)
    print("    发给别人：给 %s 这一个文件 + SHA256SUMS" % zpath.name)
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 NLC")
    ap.add_argument("--out", default=str(DIST), help="输出目录（默认 packaging/dist）")
    ap.add_argument("--keep-staging", action="store_true")
    ap.add_argument("--windows", action="store_true",
                    help="打 Windows 便携版（.zip）。不加这个开关就是原来的 Linux tar.gz + .run")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # ★ Windows 走另一条分支，返回后直接结束 —— 下面的 Linux 逻辑一行都不碰
    if args.windows:
        return build_windows(out)

    ver = version()
    top = "nlc-%s" % ver

    print("═══ 打包 NLC %s ═══\n" % ver)
    items = collect()
    print("  收录 %d 个文件" % len(items))

    tgz = out / ("%s.tar.gz" % top)
    build_tarball(tgz, top, items)
    print("  ✔ %s  (%.1f KB)" % (tgz.name, tgz.stat().st_size / 1024))

    run = out / ("%s.run" % top)
    build_run(run, ver, top, tgz)
    print("  ✔ %s  (%.1f KB，自解压单文件)" % (run.name, run.stat().st_size / 1024))

    sums = out / "SHA256SUMS"
    sums.write_text("%s  %s\n%s  %s\n" % (sha256(tgz), tgz.name, sha256(run), run.name),
                    encoding="utf-8")
    print("  ✔ %s" % sums.name)

    # 顺手检查：包里的 shell 脚本语法是否都过
    # ⚠️ 这台机器是 Windows：os.system 走的是 cmd.exe，找不到 sh，
    #    所以直接调 os.system 会全部报"系统找不到指定的路径"——那是检查方式的问题，不是脚本的问题。
    #    正确做法是先确认 sh 存在，再走 subprocess。
    bad = 0
    print("\n  包里脚本的语法检查：")
    sh = shutil.which("sh")
    if not sh:
        print("    跳过（这台机器上没有 sh，请在 Linux 上复核）")
    else:
        for arc, src, _ in items:
            if not arc.endswith(".sh"):
                continue
            r = subprocess.run([sh, "-n", str(src)], capture_output=True, text=True)
            if r.returncode != 0:
                print("    ✘ %s  %s" % (arc, (r.stderr or "").strip()[:80]))
                bad += 1
        print("    %s" % ("全部通过 ✔" if not bad else "%d 个有问题 ✘" % bad))

    # ★ 换行检查：包里的文本文件绝不能带 CRLF（详见 TEXT_EXTS 处的说明）
    print("\n  换行符检查（文本文件必须全是 LF）：")
    crlf = []
    with tarfile.open(tgz) as tf:
        for m in tf.getmembers():
            if m.isfile() and _is_text(m.name):
                if b"\r\n" in tf.extractfile(m).read():
                    crlf.append(m.name)
    if crlf:
        print("    ✘ 仍是 CRLF：%s" % "、".join(crlf[:5]))
        bad += 1
    else:
        print("    全部是 LF ✔")

    print("\n  下一步：")
    print("    本机装：sh %s" % run.name)
    print("    拷给别人：只给 %s 一个文件就行" % run.name)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
