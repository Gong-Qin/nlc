# -*- coding: utf-8 -*-
"""NLC 平台适配层 —— 让同一套代码在 Linux 与 Windows 上都跑得对。

为什么单独拉一层（而不是在各地写 `if os.name == "nt"`）：
    这套东西的定位是**可移植的开源项目**。平台差异只应该集中在一个文件里，
    别处一律从这里取答案 —— 否则每加一个平台就要满地找 if。

本模块只回答四个问题：
    1. **配置放哪**   —— Windows 走 `%APPDATA%\\nlc`，Linux 走 `~/.config/nlc`
    2. **判哪种命令行** —— cmd / PowerShell / bash，决定风险规则与提示词的措辞
    3. **怎么起 Python** —— Windows 是 `python`，Linux 是 `python3`
    4. **怎么给文件收权限** —— POSIX 用 chmod 600；Windows 上 chmod 只认只读位，
                             改用 icacls 去掉继承并只留当前用户

⚠️ 本模块**不导入 nlc 包内任何其它模块**，以免形成循环导入。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

__all__ = [
    "IS_WINDOWS", "IS_POSIX", "OS_LABEL",
    "SHELL_CMD", "SHELL_POWERSHELL", "SHELL_BASH", "SHELLS",
    "CONFIG_DIR", "default_shell", "active_shell", "set_shell",
    "shell_label", "shell_hint", "python_exe", "python_cmd",
    "secure_file", "secure_dir", "launcher_dir", "is_elevated",
]

IS_WINDOWS = os.name == "nt"
IS_POSIX = not IS_WINDOWS
OS_LABEL = ("Windows" if IS_WINDOWS else "Linux") + " (" + sys.platform + ")"

SHELL_CMD = "cmd"
SHELL_POWERSHELL = "powershell"
SHELL_BASH = "bash"
SHELLS = (SHELL_CMD, SHELL_POWERSHELL, SHELL_BASH)


# --------------------------------------------------------------------------- #
# 1. 配置放哪
# --------------------------------------------------------------------------- #
def _default_config_dir() -> Path:
    """按平台给出配置目录。允许用环境变量 NLC_CONFIG_DIR 整体覆盖。"""
    env = os.environ.get("NLC_CONFIG_DIR")
    if env:
        return Path(env)
    if IS_WINDOWS:
        # ★ Windows 的正确位置是 %APPDATA%（即 C:\Users\<你>\AppData\Roaming）。
        #   不要沿用 ~/.config —— 那是 XDG 约定，Windows 上没人认得。
        base = os.environ.get("APPDATA")
        if not base:
            base = str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "nlc"
    return Path.home() / ".config" / "nlc"


CONFIG_DIR = _default_config_dir()


# --------------------------------------------------------------------------- #
# 2. 判哪种命令行
# --------------------------------------------------------------------------- #
def default_shell() -> str:
    """按平台给出默认的命令行方言。"""
    if IS_WINDOWS:
        # Windows 上先默认 cmd：它是最保守的选择（不认识 PowerShell 语法，
        # 因此不会把 PowerShell 的管道符当成别的东西）。用户可以改。
        return SHELL_CMD
    return SHELL_BASH


_shell: Optional[str] = None


def set_shell(kind: Optional[str]) -> str:
    """切换当前方言。传 None 或非法值则回到平台默认。大小写不敏感。"""
    global _shell
    k = (kind or "").strip().lower()
    if k == "ps":
        k = SHELL_POWERSHELL
    _shell = k if k in SHELLS else None
    return active_shell()


def active_shell() -> str:
    """当前生效的方言（用户设置优先，其次环境变量，最后平台默认）。"""
    if _shell:
        return _shell
    env = (os.environ.get("NLC_SHELL") or "").strip().lower()
    if env in SHELLS:
        return env
    return default_shell()


#: 给人看的方言名字
#: ⚠️ 这里**不要**再带上内部 id（cmd / powershell / bash）——
#:    调用方通常写成 `%s（%s）` 把名字和 id 一起打，带上就重复了。
_SHELL_LABEL: Dict[str, str] = {
    SHELL_CMD: "命令提示符",
    SHELL_POWERSHELL: "PowerShell",
    SHELL_BASH: "bash / Linux shell",
}

#: 写进提示词的措辞 —— 模型要知道自己在给哪种命令行写命令
_SHELL_HINT: Dict[str, str] = {
    SHELL_CMD: "Windows 的**命令提示符（cmd.exe）**命令",
    SHELL_POWERSHELL: "Windows 的 **PowerShell** 命令",
    SHELL_BASH: "**Linux shell（bash）**命令",
}


def shell_label(kind: Optional[str] = None) -> str:
    return _SHELL_LABEL.get(kind or active_shell(), kind or active_shell())


def shell_hint(kind: Optional[str] = None) -> str:
    """提示词里用的措辞。"""
    return _SHELL_HINT.get(kind or active_shell(), _SHELL_HINT[SHELL_BASH])


# --------------------------------------------------------------------------- #
# 3. 怎么起 Python
# --------------------------------------------------------------------------- #
def python_exe() -> str:
    """当前解释器的可执行文件路径（写进安装脚本、MCP 客户端配置用）。"""
    return sys.executable or python_cmd()


def python_cmd() -> str:
    """命令行的惯用叫法：Windows 是 python，Linux 是 python3。"""
    return "python" if IS_WINDOWS else "python3"


# --------------------------------------------------------------------------- #
# 4. 权限
# --------------------------------------------------------------------------- #
def secure_dir(path: Path) -> List[str]:
    """尽力把这个目录收成「只有本人可访问」。返回实际执行的说明（给界面显示用）。

    ⚠️ 如实记录一个差异：
        - POSIX：`chmod 700` 真的生效。
        - Windows：**`os.chmod` 只认只读位，改不了 ACL**，所以 600/700 在
          Windows 上是**空操作**。这不是 bug，是 NTFS 的权限模型和 POSIX 不同。
          本函数改用 `icacls` 去掉继承、只留当前用户 —— 但 icacls 可能失败
          （盘是 FAT32、路径太长、被组策略限制），失败时**如实返回**而不假装成功。
    """
    path = Path(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return ["创建目录失败：%s" % exc]

    if IS_POSIX:
        try:
            os.chmod(path, 0o700)
            return ["chmod 700 %s" % path]
        except OSError as exc:
            return ["chmod 失败：%s" % exc]

    return _icacls(path, is_dir=True)


def secure_file(path: Path) -> List[str]:
    """把一个文件收成「只有本人可读写」。返回实际执行的说明。"""
    path = Path(path)
    if not path.exists():
        return []
    if IS_POSIX:
        try:
            os.chmod(path, 0o600)
            return ["chmod 600 %s" % path]
        except OSError as exc:
            return ["chmod 失败：%s" % exc]
    return _icacls(path, is_dir=False)


def _icacls(path: Path, is_dir: bool) -> List[str]:
    """Windows：用 icacls 去掉继承、只给当前用户完全控制。

    故意**不抛异常**：密钥文件保护失败不应该让程序崩掉，
    但必须返回真实情况让人知道 —— 所以返回值是一串「做了什么」。
    """
    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    who = ("%s\\%s" % (domain, user)) if domain and user else (user or "")
    if not who:
        return ["拿不到当前用户名，未收权限（Windows 上 chmod 不生效，请手动检查）"]

    steps: List[str] = []
    try:
        import subprocess

        cmds = [
            ["icacls", str(path), "/inheritance:r"],
            ["icacls", str(path), "/grant:r", "%s:(F)" % who],
        ]
        if is_dir:
            cmds = [
                ["icacls", str(path), "/inheritance:r"],
                ["icacls", str(path), "/grant:r", "%s:(OI)(CI)(F)" % who],
            ]
        for c in cmds:
            # ★ 故意**不用 text=True**：icacls 的输出走的是 Windows 的 OEM 代码页
            #   （中文系统上是 GBK），而 Python 在 UTF-8 模式下会按 utf-8 去解码，
            #   结果是解码线程抛 UnicodeDecodeError —— 症状是明明成功了却打印一堆
            #   异常堆栈。我们只关心返回码，所以直接拿字节、不解码。
            p = subprocess.run(c, capture_output=True, timeout=15)
            steps.append(" ".join(c[1:]) + ("  [ok]" if p.returncode == 0 else "  [失败]"))
    except Exception as exc:                             # noqa: BLE001 - 权限收紧失败不该崩
        steps.append("icacls 不可用：%s" % exc)
    return steps


# --------------------------------------------------------------------------- #
# 5. 杂项
# --------------------------------------------------------------------------- #
def launcher_dir() -> Path:
    """便携安装的默认目标目录。"""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Programs" / "nlc"
    return Path.home() / ".local" / "share" / "nlc"


def is_elevated() -> bool:
    """当前是否以管理员身份运行（Windows 专用；Linux 上恒为 False）。

    为什么需要：管理员身份下 `del /f /s /q C:\\` 这类命令的破坏力会放大，
    风险提示要能说出这一点。
    """
    if not IS_WINDOWS:
        return bool(os.environ.get("NLC_ASSUME_ROOT"))
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())   # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        return False
