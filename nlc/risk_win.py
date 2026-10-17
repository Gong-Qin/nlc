# -*- coding: utf-8 -*-
"""NLC 的 Windows 风险规则集（cmd + PowerShell）。

为什么单独一个文件：
    Linux 的那套规则（`rm -rf /`、`mkfs`、`chmod -R 777 /`）在 Windows 上**一条都不会命中**。
    照搬过来，Windows 版就成了"什么都说安全"的空壳 —— 这比没有更危险
    （用户会以为它把过关了）。所以 Windows 规则必须单独、成体系地写。

设计原则（与 Linux 侧完全一致）：
    1. **判定由本地规则引擎做，不交给大模型。**
    2. 判定优先级：**禁止 > 需确认 > 放行**。
    3. 拿不准的一律「需确认」，不默认放行。

本模块**不导入 risk.py**（避免循环导入）：
    它只返回朴素的元组 `(level, reason, rule_id, category, targets)`，
    由 risk.py 组装成 Assessment。level 的取值就是 "allow" / "confirm" / "block"，
    与 risk.py 里的常量一致。
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

__all__ = [
    "Finding", "WIN_SAFE_READONLY", "check_block", "check_confirm", "check_allow",
    "is_win_critical", "win_wrapper_inner", "is_win_shell_wrapper",
    "normalize_path", "WIN_SAFE_SHELLS",
]

Finding = Tuple[str, str, str, str, list]

ALLOW, CONFIRM, BLOCK = "allow", "confirm", "block"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _unq(s: str) -> str:
    return (s or "").replace('"', "").replace("'", "").strip()


def normalize_path(raw: str) -> str:
    """把路径归一化：去引号、正反斜杠统一、折叠重复分隔符。"""
    t = _unq(raw).replace("/", "\\")
    t = re.sub(r"\\{2,}", r"\\", t)
    return t


def _has_any(words: Sequence[str], flags: Sequence[str]) -> bool:
    low = [w.lower() for w in words]
    return any(f.lower() in low for f in flags)


def _norm_sub(w: str) -> str:
    """把子命令名归一化：`/query`、`-query`、`query` 看成同一个。

    为什么需要：切分后的开关是带前缀的（`schtasks /query` 得到 `/query`），
    不剥掉前缀就会漏配白名单，把只读查询误报成红色。
    """
    return (w or "").lower().lstrip("/-")


def _has_sw(words: Sequence[str], letters: str) -> bool:
    """cmd 风格的斜杠开关：`/f`、`/s`、`/q` 或合写 `/f/s`。"""
    for w in words[1:]:
        if not w.startswith("/"):
            continue
        body = w.lstrip("/").lower().replace("/", "")
        if body and all(ch in body for ch in letters.lower()):
            return True
    return False


def _operands(words: Sequence[str]) -> List[str]:
    """去掉命令名、选项（`-x` / `/x` / `--x`）、以及 flag 的值之后剩下的操作对象。"""
    out: List[str] = []
    for w in words[1:]:
        if w.startswith("-") and len(w) > 1:
            continue
        if w.startswith("/") and len(w) > 1 and "\\" not in w:
            continue
        out.append(w)
    return out


# --------------------------------------------------------------------------- #
# ★ 关键路径：删到这里就熔断（对应 Linux 的 rm -rf /）
# --------------------------------------------------------------------------- #
#: 环境变量形态的「根」，删了等于把系统/用户资料连根拔
_CRITICAL_ENV = {
    "%systemroot%", "%windir%", "%systemdrive%", "%systemroot%\\",
    "%programfiles%", "%programfiles(x86)%", "%programfiles%\\",
    "%programdata%", "%allusersprofile%", "%userprofile%", "%homepath%",
    "%appdata%", "%localappdata%", "%homedrive%",
}

#: 环境变量名（不带壳），用于识别 PowerShell 的 `$env:XXX` 写法
_CRITICAL_ENV_NAMES = {
    "systemroot", "windir", "systemdrive", "programfiles", "programfiles(x86)",
    "programdata", "allusersprofile", "userprofile", "homepath",
    "appdata", "localappdata", "homedrive",
}

#: 关键目录（相对盘符的路径，小写、无反斜杠开头）
_CRITICAL_DIRS = {
    "windows", "windows\\system32", "windows\\syswow64", "windows\\system",
    "program files", "program files (x86)", "programdata",
    "users", "recovery", "boot", "$recycle.bin", "system volume information",
    "perflogs", "msocache", "config.msi",
}

#: 系统关键文件：被覆盖/删除就开不了机
_CRITICAL_FILES = {
    "bootmgr", "bootnxt", "boot.ini", "ntldr", "ntdetect.com",
    "pagefile.sys", "hiberfil.sys", "swapfile.sys",
    "windows\\system32\\config\\sam", "windows\\system32\\config\\system",
    "windows\\system32\\config\\security", "windows\\system32\\config\\software",
    "windows\\system32\\hal.dll", "windows\\system32\\ntoskrnl.exe",
    "windows\\system32\\winload.exe", "windows\\system32\\winload.efi",
}


def is_win_critical(raw: str) -> Optional[str]:
    """判断一个目标是否属于「关键路径」。命中返回人类可读的名字，否则 None。"""
    t = normalize_path(raw)
    if not t:
        return None
    low = t.lower()
    bare = low.rstrip("\\")
    if not bare:
        return None

    # 1) 通配符（`*` 或 `*.*`）本身就是"这一层全删"
    wild = bare.split("\\")[-1]
    is_wild = wild in ("*", "*.*", "?", "*. *", "**")

    # 2) 盘符根：C: / C:\ / C:\*
    if re.fullmatch(r"[a-z]:", bare):
        return "盘符根目录 %s\\" % t[:2].upper()
    if re.fullmatch(r"[a-z]:\\\*+(\.\*)?", low):
        return "盘符根目录下的通配删除 %s" % t
    if bare in ("\\", ""):
        return "当前盘根目录"

    # 3) 环境变量形态（cmd 的 %X% 与 PowerShell 的 $env:X 都认）
    if bare in {e.rstrip("\\") for e in _CRITICAL_ENV}:
        return "系统/用户根目录（%s）" % raw
    m_env = re.fullmatch(r"\$\{?env:([a-z0-9_()]+)\}?", low)
    if m_env and m_env.group(1) in _CRITICAL_ENV_NAMES:
        return "系统/用户根目录（%s）" % raw
    if low in ("$home", "${home}", "$env:home", "$pshome"):
        return "系统/用户根目录（%s）" % raw

    # 4) 相对当前目录
    if bare in (".", "..") or bare.startswith("..\\") and bare == "..":
        return "当前工作目录（%s）" % t

    # 5) 去掉盘符与开头的反斜杠，得到相对路径
    rel = re.sub(r"^[a-z]:", "", bare)
    rel = rel.lstrip("\\")
    if not rel:
        return "盘符根目录 %s\\" % bare[:2].upper()

    if rel in _CRITICAL_DIRS:
        return "系统关键目录 %s" % t
    if rel in _CRITICAL_FILES:
        return "系统关键文件 %s" % t

    # 6) 盘符根下的通配：C:\Windows\*
    if is_wild and rel.rstrip("\\*") in _CRITICAL_DIRS:
        return "系统关键目录下的通配删除 %s" % t

    # 7) 用户资料根：C:\Users\某人 （把整个用户目录连根删）
    if re.fullmatch(r"users\\[^\\]+", rel):
        return "用户资料根目录 %s" % t

    # 8) C:\Windows 下的**直接子项**（`del C:\Windows\notepad.exe`）
    #    或者 C:\Windows 下的通配删除 —— 这两类都足以让系统出问题。
    #    ⚠️ 只到"第一层"，`C:\Windows\Temp\x.txt` 这种正常清理不会误伤。
    if rel.startswith("windows\\"):
        depth = rel.count("\\")
        if depth == 1 or (is_wild and depth <= 2):
            return "系统目录 C:\\Windows 下的关键内容 %s" % t

    for d in ("windows\\system32", "windows\\syswow64"):
        if rel == d or rel.startswith(d + "\\"):
            return "系统关键目录 %s" % t

    return None


def _redirect_target_win(words: Sequence[str]) -> Optional[str]:
    joined = " ".join(words)
    m = re.search(r">>?\s*([^\s>]+)", joined)
    return normalize_path(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# 词表
# --------------------------------------------------------------------------- #
#: ★ 只读、无副作用（对应 cmd 与 PowerShell 两边的等价物）
WIN_SAFE_READONLY = {
    # —— cmd / 通用 ——
    "dir", "cd", "chdir", "type", "echo", "ver", "vol", "date", "time", "cls",
    "title", "color", "prompt", "path", "set", "chcp", "verify", "help",
    "whoami", "hostname", "ipconfig", "ping", "tracert", "pathping", "nslookup",
    "arp", "getmac", "systeminfo", "tasklist", "find", "findstr", "more",
    "sort", "fc", "comp", "tree", "where", "openfiles", "query", "quser",
    "qwinsta", "gpresult", "driverquery", "assoc", "ftype", "timeout",
    # —— PowerShell 常用只读别名 ——
    "ls", "cat", "gci", "gc", "gps", "gi", "gp", "gm", "gl", "gv", "gcm",
    "gdr", "gsv", "history", "h", "clear-host", "write-output",
    "write", "echo", "select", "sleep", "start-sleep",
}

#: ★ 只读、但**能执行后续命令**的包装器 —— 必须剥掉再判，否则就是绕过口子。
#:   对应 Linux 侧的 env / sudo / timeout。
WIN_DYNAMIC_WRAPPERS = {
    "cmd": ("/c", "/k", "/r"),
    "powershell": ("-command", "-c", "-encodedcommand", "-e", "-ec", "-file"),
    "pwsh": ("-command", "-c", "-encodedcommand", "-e", "-ec", "-file"),
}

#: PowerShell 的「动态执行」cmdlet：内容静态看不出来
WIN_DYNAMIC_EXEC = {"invoke-expression", "iex", "invoke-command", "icm"}

#: 会「逐项执行一段命令」的（等价于 Linux 的 xargs）。
#: 这些**一律需确认** —— 内容在静态层面完全看不出来。
WIN_FOREACH_LIKE = {"forfiles", "for", "start-job", "invoke-item",
                    "invoke-command", "icm"}

#: ★ 但 `ForEach-Object` 要区别对待。
#: 它是 PowerShell 管道里最常见的"逐项处理"，`... | ForEach-Object { 只读表达式 }`
#: 是**完全无副作用**的写法 —— 一律标红会把它变成狼来了：
#: 用户见惯红色之后，真正危险的那条也会被点过去。
#: ⇒ 做法：看脚本块里出现了什么。全是只读 cmdlet / 表达式 → 放行；出现会改东西的 → 需确认。
_PS_PIPELINE_FOREACH = {"foreach-object", "foreach"}

#: 出现在 ForEach 脚本块里、就意味着"这段会改动东西"的命令
_PS_BLOCK_DANGEROUS = re.compile(
    r"(?:^|[\s|;{(])(?:"
    r"remove-item|ri|del|erase|rd|rmdir|move-item|mi|copy-item|cpi|rename-item|rni|"
    r"remove-|set-content|out-file|add-content|clear-content|new-item|ni|"
    r"start-process|stop-process|spps|kill|start-job|stop-job|"
    r"stop-service|restart-service|set-service|new-service|remove-service|"
    r"invoke-expression|iex|invoke-command|icm|invoke-webrequest|iwr|"
    r"invoke-restmethod|irm|start-bitsTransfer|"
    r"stop-computer|restart-computer|set-executionpolicy|set-mpPreference|"
    r"format-volume|clear-disk|initialize-disk|new-partition|remove-partition|"
    r"set-acl|set-owner|takeown|icacls|cacls|attrib|"
    r"reg|netsh|schtasks|bcdedit|vssadmin|wbadmin|diskpart|format|"
    r"cmd|powershell|pwsh|shutdown|logoff|runas|mshta|wscript|cscript|"
    r"winget|choco|scoop|msiexec|wusa|certutil"
    r")(?=[\s|;{}()]|$)",
    re.I)


def _foreach_is_readonly(words: Sequence[str], raw: str) -> bool:
    """这段 ForEach 是不是「只逐项处理、不改动任何东西」。

    判据有两个，缺一不可：
        1. 脚本块里**没有**出现会改动系统的命令（`_PS_BLOCK_DANGEROUS`）
        2. 确实带了脚本块（有 `{`）—— 光写 `ForEach-Object` 后面跟别的东西更可疑

    ⚠️ 必须用 `" ".join(words)` 而不是 `raw` 去做正则：
        `raw` 是把词块**直接粘起来**的（`ForEach-Object{Remove-Item$_.FullName…`），
        词与词之间没有空格，`\\b` 之类的词边界就全都匹配不上 ——
        结果是危险的 Remove-Item 也被判成"只读"。这个坑踩过一次。
    """
    if "{" not in raw:
        return False
    body = " ".join(words)
    # 把命令名本身抠掉，免得 `ForEach-Object` 里的 `foreach` 自己撞上关键词
    for w in words[:1]:
        body = body.replace(w, "", 1)
    return not _PS_BLOCK_DANGEROUS.search(body)

#: Windows 上没有 shell 的"可执行体"—— 出现即代表要当场跑东西
WIN_SAFE_SHELLS = {"bash", "wsl", "git-bash"}

#: 破坏性删除。
#: ★ 故意把 Linux 的 rm / shred / unlink 也放进来 —— Windows 上装了 Git Bash 或
#:   WSL 的人是能敲出这些命令的，只按 Linux 规则判会漏掉 Windows 的关键路径。
WIN_DELETE = {"del", "erase", "rd", "rmdir", "deltree", "sdelete",
              "remove-item", "ri", "rm", "shred", "unlink"}

#: 会改动文件内容的
WIN_WRITE = {"copy", "xcopy", "robocopy", "replace", "move", "ren", "rename",
             "attrib", "compact", "set-content", "out-file", "add-content",
             "clear-content", "new-item", "ni", "rename-item", "rni",
             "move-item", "mi", "copy-item", "cpi", "tee-object",
             "cp", "mv", "tee", "truncate", "install"}

#: 权限 / 属主变更
WIN_PERM = {"icacls", "cacls", "takeown", "attrib", "set-acl", "get-acl",
            "set-owner", "icacls.exe"}

#: 提权 / 以他人身份运行
WIN_ELEVATE = {"runas", "runas.exe"}

#: 结束进程
WIN_KILL = {"taskkill", "tskill", "stop-process", "spps", "sppsvc"}

#: 关机 / 重启 / 注销 / 服务与电源
WIN_POWER = {"shutdown", "logoff", "stop-computer", "restart-computer",
             "stop-service", "restart-service", "start-service", "set-service",
             "new-service", "remove-service", "sc", "net", "powercfg"}

#: 磁盘 / 分区 / 引导
WIN_DISK = {"chkdsk", "dism", "convert", "diskcomp", "diskcopy", "mountvol",
            "fsutil", "cipher", "diskpart", "format", "vssadmin", "wbadmin",
            "bcdedit", "bootrec", "bootsect", "defrag", "optimize-volume",
            "format-volume", "clear-disk", "initialize-disk", "new-partition",
            "remove-partition", "set-disk", "get-disk", "resize-partition",
            "repair-volume", "mount-diskimage", "dismount-diskimage"}

#: 系统 / 配置 / 账户
WIN_SYS = {"reg", "regedit", "regedt32", "schtasks", "wmic", "netsh",
           "slmgr", "bcdedit", "sfc", "new-localuser", "set-localuser",
           "remove-localuser", "add-localgroupmember", "remove-localgroupmember",
           "new-localgroup", "set-executionpolicy", "set-mppreference",
           "set-itemproperty", "sp", "new-itemproperty", "remove-itemproperty",
           "set-timezone", "set-culture", "enable-windowsoptionalfeature",
           "disable-windowsoptionalfeature", "dism", "msiexec", "wusa",
           "certutil", "runas"}

#: 包管理 / 安装
#: ⚠️ 故意的：**不把 python / py 放进来**。它们不是"装软件"，
#:    而是"当场跑一段代码"，按「未知命令 ⇒ 需确认」处理更准。
WIN_PKG = {"winget", "choco", "scoop", "npm", "pip", "pip3", "nuget",
           "msiexec", "wusa", "conda"}

#: 需要联网
WIN_NET = {"curl", "wget", "bitsadmin", "ftp", "tftp", "ssh", "scp", "sftp",
           "telnet", "invoke-webrequest", "iwr", "invoke-restmethod", "irm",
           "nc", "ncat", "certutil"}

#: 受保护文件（用户级配置，改错难察觉）
WIN_PROTECTED_NAMES = {
    ".git", ".ssh", ".vscode", ".idea", ".npmrc", ".gitconfig", ".env",
    ".gitignore", ".gitmodules", "appdata\\roaming\\microsoft\\windows\\"
    "start menu", "hosts", "system.ini", "win.ini", "boot.ini",
}


# --------------------------------------------------------------------------- #
# 包装器剥离 —— 堵住 "cmd /c 后面跟什么都能跑" 这个口子
# --------------------------------------------------------------------------- #
def is_win_shell_wrapper(words: Sequence[str]) -> bool:
    if not words:
        return False
    c = words[0].lower()
    return c in WIN_DYNAMIC_WRAPPERS or c in WIN_DYNAMIC_EXEC or c in ("start", "start-process")


def win_wrapper_inner(words: Sequence[str]) -> Optional[str]:
    """若开头是 cmd / powershell 之类的包装器，返回它**真正要执行**的那段字符串。

    这是 Windows 侧最重要的一道防线：
        `cmd /c del /f /s /q C:\\`   如果只看第一个词是 `cmd`，就会被当成安全。
        `powershell -Command "Remove-Item -Recurse -Force C:\\"` 同理。
    """
    if not words:
        return None
    c = words[0].lower()
    low = [w.lower() for w in words]

    # cmd /c | /k <rest>
    if c == "cmd":
        for i, w in enumerate(low[1:], start=1):
            if w in WIN_DYNAMIC_WRAPPERS["cmd"]:
                rest = " ".join(words[i + 1:])
                return _unq(rest) if rest.strip() else None
        return None

    # powershell / pwsh -Command <rest>
    if c in ("powershell", "pwsh"):
        for i, w in enumerate(low[1:], start=1):
            if w in WIN_DYNAMIC_WRAPPERS["powershell"]:
                rest = " ".join(words[i + 1:])
                return _unq(rest) if rest.strip() else None
        return None

    # iex / Invoke-Expression <rest>
    if c in WIN_DYNAMIC_EXEC:
        rest = " ".join(words[1:])
        return _unq(rest) if rest.strip() else None

    # start <窗口标题?> <程序> <参数…>  /  Start-Process <程序> <参数…>
    if c in ("start", "start-process"):
        rest = list(words[1:])
        while rest and (rest[0].startswith("-") or rest[0].startswith("/")):
            flag = rest.pop(0).lower()
            if flag in ("-filepath", "-path", "-argumentlist", "-args") and rest:
                continue
        # 第一个残留值若是被引号包起来的标题（cmd 的 start "标题" 程序），跳过
        return " ".join(rest) if rest else None

    return None


# --------------------------------------------------------------------------- #
# 禁止档
# --------------------------------------------------------------------------- #
#: 无条件禁止：这些一执行就是不可逆的灾难
_WIN_HARD_BLOCK_DIRECT = [
    ("format", "格式化磁盘分区。分区上的数据会**全部丢失**，而且基本找不回来。",
     "WBLOCK-01", "格式化磁盘"),
    ("diskpart", "磁盘分区工具，能直接清空整块硬盘（clean 一条命令就抹掉分区表）。",
     "WBLOCK-02", "磁盘分区操作"),
    ("bootrec", "修复/重建引导记录。用错参数会让系统**开不了机**。",
     "WBLOCK-03", "引导记录操作"),
    ("bootsect", "改写引导扇区。写错目标会让系统**开不了机**。",
     "WBLOCK-03", "引导记录操作"),
    ("clear-disk", "清空整块磁盘。上面所有分区和数据都会消失。",
     "WBLOCK-02", "磁盘分区操作"),
    ("initialize-disk", "重新初始化磁盘，会改写分区表。",
     "WBLOCK-02", "磁盘分区操作"),
    ("format-volume", "格式化卷。那个卷上的数据会全部丢失。",
     "WBLOCK-01", "格式化磁盘"),
]

#: 带特定参数才升级为禁止的
def _win_block_by_args(words: Sequence[str], raw: str) -> Optional[Finding]:
    c = words[0].lower() if words else ""
    low = [w.lower() for w in words]

    # —— 删除到关键路径 ——
    if c in WIN_DELETE:
        # ★ 候选目标要同时试两种切法：
        #   ① 每个参数单独看（`del C:\Windows`）
        #   ② 参数拼起来看 —— 路径里有空格时会被切成两个参数，
        #      `rd /s /q C:\Program Files` 的第二个参数只剩 `Files`，
        #      不拼起来就认不出这是 C:\Program Files。
        cands = list(_operands(words))
        tail = " ".join(cands).strip()
        if tail:
            cands.append(tail)
        for w in cands:
            hurt = is_win_critical(w)
            if hurt:
                return (BLOCK,
                        "这会删除「%s」—— 属于关键路径，删掉系统就没了，而且不进回收站。"
                        % hurt,
                        "WBLOCK-04", "关键路径删除", [hurt])

    # —— 改引导配置（删一条启动项就可能开不了机）——
    if c == "bcdedit" and any(_norm_sub(w) in ("delete", "deletevalue", "import",
                                               "restore", "set")
                              for w in words[1:]):
        joined = " ".join(w.lower() for w in words)
        hard = (_norm_sub(words[1]) in ("delete", "deletevalue", "import", "restore")
                or "recoveryenabled" in joined or "bootstatuspolicy" in joined)
        if hard:
            return (BLOCK, "这会改动「启动配置数据」—— 删错或改错一条，系统就**开不了机**了。",
                    "WBLOCK-03", "引导配置修改", ["bcdedit"])
    # —— 通配删除整个盘 ——
    if re.search(r"(?:del|erase|rd|rmdir|remove-item)\b.*[a-z]:\\?\*", raw, re.I):
        return (BLOCK, "这条命令在删盘符根下的通配内容，等于清空整个盘。",
                "WBLOCK-04", "关键路径删除", [raw])

    # —— 关闭实时防护 / 删除还原点：勒索软件的典型前置动作 ——
    if c in ("set-mppreference",) and any("disablerealtimemonitoring" in w or "disable" in w
                                          for w in low):
        return (BLOCK, "这会关闭 Windows Defender 的实时防护，机器会立刻失去病毒防护。",
                "WBLOCK-05", "关闭安全防护", ["Set-MpPreference"])
    if c in ("vssadmin",) and "delete" in low and "shadows" in low:
        return (BLOCK, "这会删除全部卷影副本（系统还原点），正是勒索软件的标准前置步骤。",
                "WBLOCK-05", "删除系统还原点", ["vssadmin delete shadows"])
    if c in ("wbadmin",) and "delete" in low:
        return (BLOCK, "这会删除系统备份与还原点，出事之后就没法恢复了。",
                "WBLOCK-05", "删除系统备份", ["wbadmin delete"])
    if c in ("disable-computerrestore", "remove-computerrestorepoint"):
        return (BLOCK, "这会关掉/删除系统还原点，出事之后就没法恢复了。",
                "WBLOCK-05", "删除系统还原点", [c])

    # —— 改注册表关键分支 ——
    if c in ("reg", "regedit", "regedt32") and len(words) > 2:
        sub = low[1] if len(low) > 1 else ""
        joined = " ".join(low)
        if sub in ("delete", "add", "import", "restore") and not _has_any(words, ["/?"]):
            for key in ("hklm\\system", "hklm\\sam", "hklm\\security",
                        "hklm\\software\\microsoft\\windows nt",
                        "hklm\\software\\microsoft\\windows\\currentversion\\run"):
                if key in joined:
                    return (BLOCK, "这会改动注册表里的系统关键分支「%s」，改错系统会起不来。" % key,
                            "WBLOCK-06", "注册表关键分支", [key])

    # —— 把系统盘权限重置 ——
    if c in WIN_PERM:
        for w in _operands(words):
            hurt = is_win_critical(w)
            if hurt:
                return (BLOCK, "这会把「%s」的权限整个改掉，系统很可能立刻无法启动。" % hurt,
                        "WBLOCK-07", "改写关键目录权限", [hurt])

    # —— 覆盖写入系统关键文件 ——
    tgt = _redirect_target_win(words)
    if tgt and is_win_critical(tgt):
        return (BLOCK, "这会覆盖写入「%s」—— 系统关键文件被改写，机器会起不来。" % tgt,
                "WBLOCK-08", "覆盖系统关键文件", [tgt])

    # —— 删除所有卷影 / 强删系统盘 ——
    if c in ("rd", "rmdir", "del", "erase") and _has_sw(words, "s") and \
            any(is_win_critical(w) for w in _operands(words)):
        hurt = next(is_win_critical(w) for w in _operands(words) if is_win_critical(w))
        return (BLOCK, "这会递归强删「%s」，删除无法撤销。" % hurt,
                "WBLOCK-04", "关键路径删除", [hurt])

    return None


def check_block(words: Sequence[str], raw: str) -> Optional[Finding]:
    if not words:
        return None
    c = (words[0] or "").lower()
    for name, why, rid, cat in _WIN_HARD_BLOCK_DIRECT:
        if c == name:
            return (BLOCK, why, rid, cat, [name])
    return _win_block_by_args(words, raw)


# --------------------------------------------------------------------------- #
# 需确认档
# --------------------------------------------------------------------------- #
_WIN_SIMPLE_CONFIRM = [
    (WIN_DELETE, "WCONFIRM-01", "删除文件",
     "会删掉文件。Windows 的 del/rd **不进回收站**（PowerShell 的 Remove-Item 默认也不进），删了基本找不回来。"),
    (WIN_WRITE,  "WCONFIRM-02", "改动文件",
     "会复制/移动/改写文件，目标位置的原有内容可能被覆盖。"),
    (WIN_PERM,   "WCONFIRM-03", "权限/属主变更",
     "会改文件或目录的访问权限，改错可能导致文件被锁死、或者被别人读到。"),
    (WIN_ELEVATE, "WCONFIRM-04", "提权执行",
     "会以另一个账户（通常是管理员）的身份运行，越过当前的权限边界。"),
    (WIN_KILL,   "WCONFIRM-05", "结束进程",
     "会强制结束进程，没来得及保存的数据会丢。"),
    (WIN_POWER,  "WCONFIRM-06", "系统服务/电源操作",
     "会中断当前所有工作，或改变系统服务的运行状态。"),
    (WIN_DISK,   "WCONFIRM-07", "磁盘操作",
     "涉及磁盘、分区或引导，操作失误很容易毁数据。"),
    (WIN_SYS,    "WCONFIRM-08", "系统配置修改",
     "会改动系统设置、注册表或账户，影响范围超出当前目录。"),
]

#: 只读子命令白名单：这些工具的**这些子命令**只列信息、不改东西。
#: ⚠️ 注意 `net` 不在这里 —— 它的子命令名（user / share / localgroup）
#:    既可能"只列出"也可能"建一个"，光看名字分不出来，必须看参数个数。见 `_net_is_readonly`。
_WIN_READONLY_SUB = {
    "reg": ("query", "compare"),
    "sc": ("query", "queryex", "qc", "getdisplayname", "getkeyname", "enumdepend"),
    "schtasks": ("query",),
    "vssadmin": ("list",),
    "wbadmin": ("get",),
    "netsh": ("show", "dump"),
    "certutil": ("hashfile", "verifystore", "dump"),
    "wmic": ("list", "get"),
}

#: `net` 的只读子命令 —— 但**只有在不带额外参数时才只读**
#: （`net user` 是列出用户；`net user 张三 /add` 是建账户 ⇒ 必须确认）
_NET_LIST_SUBS = ("user", "share", "group", "localgroup", "session", "use",
                  "view", "config", "statistics", "time", "accounts", "file")


def _net_is_readonly(words: Sequence[str]) -> bool:
    if len(words) != 2:
        return False
    return words[1].lower() in _NET_LIST_SUBS


def _wmic_is_readonly(words: Sequence[str]) -> bool:
    low = [w.lower() for w in words]
    if any(k in low for k in ("call", "delete", "create", "set")):
        return False
    sub = low[1] if len(low) > 1 else ""
    return sub in ("list", "get")


def _readonly_sub_hit(c: str, words: Sequence[str]) -> bool:
    """该工具的这次调用是否属于只读子命令。"""
    if c == "net":
        return _net_is_readonly(words)
    if c == "wmic":
        return _wmic_is_readonly(words)
    subs = _WIN_READONLY_SUB.get(c)
    if not subs:
        return False
    sub = _norm_sub(words[1]) if len(words) > 1 else ""
    return sub in subs


def check_confirm(words: Sequence[str], raw: str) -> Optional[Finding]:
    if not words:
        return None
    c = (words[0] or "").lower()
    low = [w.lower() for w in words]

    # —— 动态执行：内容静态看不出来 ——
    if c in WIN_DYNAMIC_EXEC:
        return (CONFIRM, "这条命令执行的是**动态拼出来的字符串**，我无法事先看出它到底要干什么。",
                "WCONFIRM-10", "动态执行", [c])
    if c in ("cmd",) and _has_any(words, ("/c", "/k", "/r")):
        return (CONFIRM, "这是在另一个命令行窗口里执行一段字符串，我无法事先看出它的完整影响。",
                "WCONFIRM-10", "动态执行", [c])
    if c in ("powershell", "pwsh") and _has_any(words, ("-command", "-c", "-encodedcommand",
                                                        "-e", "-ec", "-file")):
        return (CONFIRM, "这是在 PowerShell 里执行一段字符串/脚本文件，我无法事先看出它的完整影响。"
                         "用 -EncodedCommand 的尤其可疑 —— 内容是刻意藏起来的。",
                "WCONFIRM-10", "动态执行", [c])
    if c in ("mshta", "wscript", "cscript"):
        return (CONFIRM, "这是脚本宿主，会执行一个脚本文件里的代码。",
                "WCONFIRM-10", "动态执行", [c])

    # —— 逐个执行（等价于 xargs）——
    if c in WIN_FOREACH_LIKE:
        return (CONFIRM, "这条命令会把输入**逐项交给另一段命令去执行**，我无法事先看清它到底会跑什么。",
                "WCONFIRM-11", "逐项执行", [c])

    # —— PowerShell 的 ForEach：要看看脚本块里到底干了什么 ——
    if c in _PS_PIPELINE_FOREACH or (c == "%" and "{" in raw):
        if _foreach_is_readonly(words, raw):
            return None                      # 只读的逐项处理 ⇒ 交给放行档
        return (CONFIRM, "这会**逐项执行一段脚本**，而且里面出现了会改动系统的命令 —— "
                         "我无法事先看清它到底会跑什么。",
                "WCONFIRM-11", "逐项执行", [c])

    # —— 只读子命令 ⇒ 交给放行档 ——
    if _readonly_sub_hit(c, words):
        return None

    # —— 改驱动器关联 / 打开图形界面 ——
    if c in ("assoc", "ftype") and len(words) > 1:
        return (CONFIRM, "这会修改文件类型关联。", "WCONFIRM-12", "修改系统关联", [c])
    if c in ("start", "start-process", "explorer", "control", "rundll32"):
        return (CONFIRM, "这会打开或启动一个程序/窗口。",
                "WCONFIRM-13", "启动程序", [c])

    # —— 包管理：只读子命令除外 ——
    if c in WIN_PKG:
        sub = _norm_sub(low[1]) if len(low) > 1 else ""
        ro = ((c == "winget" and sub in ("list", "search", "show")) or
              (c in ("pip", "pip3") and sub in ("list", "show", "freeze")) or
              (c == "npm" and sub in ("list", "ls", "view", "outdated")) or
              (c == "choco" and sub in ("list", "search", "info")) or
              (c == "scoop" and sub in ("list", "search", "info", "status")) or
              (c == "dotnet" and sub in ("list", "--list-sdks", "--info")))
        if not ro:
            return (CONFIRM, "这会安装、卸载或升级软件，会改动系统环境。",
                    "WCONFIRM-14", "包管理写操作", [c])

    # —— 需要联网 ——
    if c in WIN_NET and not (c == "curl" and _has_any(words, ("-i", "--head"))):
        return (CONFIRM, "这条命令需要联网，会产生网络流量，也可能把本机内容发出去。",
                "WCONFIRM-15", "需要联网", [c])

    # —— 覆盖写入 ——
    if re.search(r">>?(?!\s*&\d)\s*\S", raw):
        tgt = _redirect_target_win(words) or "目标文件"
        if tgt.lower() not in ("nul", "null", "/dev/null"):
            return (CONFIRM, "这会用 `>` 覆盖写入「%s」，原有内容会被直接盖掉。" % tgt,
                    "WCONFIRM-16", "覆盖写入", [tgt])

    # —— 递归强删（目标不显眼，也没到"关键路径"的程度）——
    if c in WIN_DELETE and (_has_sw(words, "s") or _has_any(words, ("-recurse",))):
        return (CONFIRM, "这会**递归**删除目录及其全部内容，且不进回收站。",
                "WCONFIRM-01", "破坏性删除", [c])

    # —— 通用档 ——
    for verbs, rid, cat, why in _WIN_SIMPLE_CONFIRM:
        if c in verbs:
            return (CONFIRM, why, rid, cat, [c])

    return None


# --------------------------------------------------------------------------- #
# 放行档
# --------------------------------------------------------------------------- #
def check_allow(words: Sequence[str], raw: str) -> Optional[Finding]:
    if not words:
        return None
    c = (words[0] or "").lower()

    # PowerShell 的 Get- 系列与 Test- 系列天然只读
    if c.startswith("get-") or c.startswith("test-"):
        return (ALLOW, "只读查询，看看而已，不会改动任何东西。", "WALLOW-01", "只读查询", [])

    # 只读的格式化 cmdlet
    if c in ("select-object", "where-object", "sort-object", "group-object",
             "measure-object", "compare-object", "format-table", "format-list",
             "format-wide", "format-custom", "out-string", "out-host",
             "convertto-json", "convertfrom-json", "convertto-csv",
             "convertfrom-csv", "select-string", "write-host"):
        return (ALLOW, "只把前面的结果整理一下显示出来，不会改动任何东西。",
                "WALLOW-02", "只读管道", [])

    # ★ 只读的 ForEach（方案见 check_confirm 里的说明）
    if (c in _PS_PIPELINE_FOREACH or (c == "%" and "{" in raw)) \
            and _foreach_is_readonly(words, raw):
        return (ALLOW, "逐项处理前面的结果，里面用的都是只读的查询命令，不会改动任何东西。",
                "WALLOW-06", "只读逐项处理", [])

    if c == "curl" and _has_any(words, ("-i", "--head")):
        return (ALLOW, "只要响应头，不下载内容。", "WALLOW-03", "只读网络查询", [])

    if c in WIN_SAFE_READONLY:
        return (ALLOW, "只读查询，看看而已，不会改动任何东西。", "WALLOW-01", "只读查询", [])

    if _readonly_sub_hit(c, words):
        return (ALLOW, "只是列出信息，不会改动任何东西。", "WALLOW-04", "只读查询", [])

    if c == "git" and len(words) > 1 and words[1].lower() in (
            "status", "log", "diff", "show", "branch", "tag", "remote", "stash"):
        return (ALLOW, "只读的 git 查询，不会改动任何东西。", "WALLOW-05", "只读 git", [])

    return None
