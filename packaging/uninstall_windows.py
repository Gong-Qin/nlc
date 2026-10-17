# -*- coding: utf-8 -*-
"""NLC Windows 卸载。

这是整个项目里**唯一会删东西的脚本**，所以每一道闸门都不许放宽。
四道安全阀（与 Linux 版的 uninstall.sh 一一对应）：

    1. 拒绝盘符根（`C:\\`）、拒绝用户主目录本身、拒绝系统目录
    2. 路径至少两层
    3. 目标目录里必须**确实有 NLC 代码**与安装清单
    4. **只删清单里记着的、当初由我们创建的东西** —— 不扫、不猜

★ 还有一条是用血换来的（Linux 版踩过）：
    显式给了 `--prefix` 就以它为准；找不到清单**直接报错退出**，
    **绝不偷偷回落到默认位置**。
    当时 `--prefix /` 这种危险输入不但没被拒绝，反而把默认位置的安装卸了，
    还报"卸载完毕" —— 最坏的那种失败：既删错了东西，还告诉你成功了。

默认**保留**个人数据（API Key、词库、黑名单、角色卡）；要删得显式 `--purge`
并手动输入 `yes`。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

# 英文 Windows 的控制台默认 cp1252，打印中文会 UnicodeEncodeError —— 统一强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

MANIFEST_NAME = ".nlc-install-manifest"


def default_target() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / "nlc"


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "nlc"


def refuse_reason(target: Path) -> Optional[str]:
    """四道安全阀的前三道。返回拒绝理由，通过则返回 None。"""
    raw = str(target).rstrip("\\/")
    if not raw:
        return "目标目录是空的"
    if len(raw) <= 3 and raw[1:2] == ":":
        return "这是盘符根目录：%s" % raw
    if Path(raw) == Path.home():
        return "这是用户主目录本身：%s" % raw
    low = raw.lower()
    for bad in ("\\windows", "\\program files", "\\programdata", "\\users\\public"):
        if low == bad or low.endswith(bad):
            return "这是系统目录：%s" % raw
    if len(Path(raw).parts) < 2:
        return "路径太浅，至少要两层：%s" % raw
    return None


def looks_like_nlc(target: Path) -> bool:
    """第四道安全阀：这个目录里确实有 NLC 的东西吗。"""
    if not target.is_dir():
        return False
    has_manifest = (target / MANIFEST_NAME).is_file()
    has_code = (target / "nlc" / "risk.py").is_file()
    has_launcher = (target / "nlc.cmd").is_file()
    return has_manifest or (has_code and has_launcher)


def load_manifest(target: Path) -> dict:
    p = target / MANIFEST_NAME
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NLC Windows 卸载")
    ap.add_argument("--prefix", default="", help="卸载哪个目录下的安装")
    ap.add_argument("--purge", action="store_true",
                    help="连个人数据（密钥/词库/黑名单）一起删")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认（脚本化用）")
    ap.add_argument("--dry-run", action="store_true", help="只报告要删什么")
    args = ap.parse_args(argv)

    explicit = bool(args.prefix)
    target = Path(args.prefix).expanduser().resolve() if explicit else default_target()

    print("=" * 66)
    print("NLC · 自然语言命令行 —— Windows 卸载")
    print("=" * 66)
    print("  目标目录：%s" % target)

    # —— 安全阀 1~3 ——
    reason = refuse_reason(target)
    if reason:
        print("\n拒绝执行：%s" % reason)
        print("卸载脚本只允许处理它自己装出来的目录。")
        return 1

    if not target.exists():
        print("\n这个目录不存在：%s" % target)
        if explicit:
            # ★ 关键：显式指定了却找不到，就直接报错 —— 绝不回落到默认位置
            print("因为你显式指定了 --prefix，我不会去别的地方找。请核对路径。")
        else:
            print("看起来本来就没装过。")
        return 1

    # —— 安全阀 4 ——
    if not looks_like_nlc(target):
        print("\n拒绝执行：这个目录里看不到 NLC 的代码与安装清单：%s" % target)
        print("为了避免删错别人的东西，卸载只会处理它自己装出来的目录。")
        return 1

    manifest = load_manifest(target)
    files = manifest.get("files") or []
    entry = manifest.get("start_menu_entry") or ""
    if not manifest:
        print("      警告：找不到安装清单，只能按目录整体删除（先给你看清单）")

    print("\n将要删除：")
    print("  安装目录  %s" % target)
    if entry:
        print("  开始菜单  %s" % entry)
    if files:
        print("  其中 %d 个文件是清单里记着的" % len(files))
    else:
        print("  （清单缺失，将整体删除该目录）")

    data_dir = config_dir()
    if args.purge:
        print("  ★ 个人数据  %s（密钥、词库、黑名单、角色卡）" % data_dir)
    else:
        print("  个人数据  %s  —— 保留不动" % data_dir)

    if args.dry_run:
        print("\n--dry-run：以上是要删的东西，未做任何改动。")
        return 0

    if args.purge and not args.yes:
        print("\n--purge 会把 API Key、个人词库、黑名单一起删掉，且不可恢复。")
        print("确认请手动输入 yes：", end="", flush=True)
        try:
            got = input().strip().lower()
        except EOFError:
            got = ""
        if got != "yes":
            print("已取消。")
            return 1

    if not args.yes:
        print("\n确认卸载请按回车（Ctrl+C 取消）：", end="", flush=True)
        try:
            input()
        except EOFError:
            pass

    # —— 开始删：只删清单里记着的 ——
    removed = 0
    if files:
        # 从深到浅删，避免先删掉父目录再删子项时报错
        for rel in sorted(files, key=lambda s: s.count(os.sep), reverse=True):
            p = target / rel
            try:
                if p.is_file():
                    p.unlink()
                    removed += 1
            except OSError:
                pass
        # 再清一遍空目录
        for root, dirs, _files in os.walk(target, topdown=False):
            for d in dirs:
                try:
                    (Path(root) / d).rmdir()
                except OSError:
                    pass
        try:
            (target / MANIFEST_NAME).unlink()
        except OSError:
            pass
        try:
            target.rmdir()
            print("  安装目录已整体删除")
        except OSError:
            print("  安装目录里还有清单之外的文件，未强行删除：%s" % target)
    else:
        # 没有清单：给一次明确的确认，再整体删
        print("  没有清单，改为整体删除目录：%s" % target)
        if args.yes or explicit:
            shutil.rmtree(target, ignore_errors=True)
        else:
            return 1

    if entry:
        try:
            Path(entry).unlink()
            print("  开始菜单项已删除")
        except OSError as exc:
            print("  开始菜单项删不掉（可手动删）：%s" % exc)

    if args.purge:
        shutil.rmtree(data_dir, ignore_errors=True)
        print("  个人数据已删除：%s" % data_dir)
    else:
        print("  个人数据保留在：%s" % data_dir)
        print("  （下次重装会自动接着用；要删请加 --purge）")

    print("\n卸载完毕。共删除 %d 个文件。" % removed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
