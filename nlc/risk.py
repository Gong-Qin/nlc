# -*- coding: utf-8 -*-
"""NLC 风险引擎 —— 三档判定（禁止 / 需确认 / 放行）。

设计原则（见《NLC规则详解_v3.1.md》第〇、二节）：
    1. AI 只翻译，不执行。执行权在用户手上。
    2. **风险判定由本地规则引擎做，不交给大模型。** 大模型被绕过也不影响安全。
    3. 判定优先级：禁止 > 需确认 > 放行。

对照实现的主流 AI Agent 默认行为：
    - OpenAI Codex    ：workspace-write + on-request；能改状态/触发外部执行的要批准
    - Claude Code     ：default 全部 shell 命令要批准；`deny > ask > allow`；
                        ★关键路径 rm 熔断（连 bypassPermissions 都拦）
    - DeepSeek Harness：workspace-write + ask；越权操作弹确认
    - Gemini CLI      ：默认全部要批准；yolo 不允许设为默认

本模块零第三方依赖，可单独单测。

已知边界（如实记录，不假装完备）：
    - `eval` / `sh -c "…"` / `source` 里的内容无法静态分析 ⇒ 一律按「需确认」处理。
    - 反引号与 `$( )` 里的内容会**递归判定**，取最严。
    - 别名、函数、变量间接引用不展开（不执行就不知道值），因此按「未知命令」处理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from . import risk_win
from .platform import SHELL_BASH, SHELL_CMD, SHELL_POWERSHELL, active_shell
from .tokenizer import Token, shell_words, split_segments, tokenize

__all__ = ["Level", "Assessment", "assess", "assess_segments", "explain",
           "ALLOW", "CONFIRM", "BLOCK"]

ALLOW = "allow"
CONFIRM = "confirm"
BLOCK = "block"

_RANK = {ALLOW: 0, CONFIRM: 1, BLOCK: 2}

#: 整条命令里一旦出现这些，无条件至少「需确认」（因为要执行的是动态字符串）
DYNAMIC_EXEC = {"eval", "source", "."}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}

#: ★ Windows 侧的「解释器」—— 管道右边出现它们，等于把下载来的东西直接执行
WIN_SHELLS = {"cmd", "powershell", "pwsh", "cscript", "wscript", "mshta"}

#: 会拉起一个新进程去跑别的东西的（跨平台都算远程执行管道的一端）
RUNNERS = SHELLS | WIN_SHELLS | {"python", "python3", "py", "perl", "ruby", "node"}


def _win(finding: Optional[risk_win.Finding]) -> Optional["Assessment"]:
    """把 Windows 规则返回的元组转成 Assessment。

    ★ 注意：**Linux 与 Windows 两套规则都跑，取最严的一档**。
      不按平台"二选一"是有意的 —— 用户在 Windows 上装了 Git Bash / WSL 时，
      `rm -rf /` 一样能跑；反过来在 Linux 上敲 `format` 也无害。
      两套都判最多让提示更保守，绝不会漏报。
    """
    if not finding:
        return None
    # 容错：规则表里允许只写 4 项（省略 targets），不给每一条都补一个空列表
    level, reason, rid, cat = finding[0], finding[1], finding[2], finding[3]
    targets = list(finding[4]) if len(finding) > 4 and finding[4] else []
    return Assessment(level, reason, rid, cat, targets)


@dataclass
class Assessment:
    """一次风险判定的结果。"""

    level: str                  # allow / confirm / block
    reason: str                 # 一句话说清为什么是这个档位
    rule_id: str = ""           # 命中的规则编号，如 "BLOCK-01"
    category: str = ""          # 类别名，如 "关键路径删除"
    targets: List[str] = field(default_factory=list)

    @property
    def executable(self) -> bool:
        """是否提供执行入口（禁止档不提供）。"""
        return self.level != BLOCK

    @property
    def needs_dialog(self) -> bool:
        """点执行后是否还要弹危险确认框。"""
        return self.level == CONFIRM


def _worse(a: Assessment, b: Assessment) -> Assessment:
    return a if _RANK[a.level] >= _RANK[b.level] else b


# --------------------------------------------------------------------------- #
# 词表
# --------------------------------------------------------------------------- #

#: 只读、无副作用的安全命令（对应 Codex 的 "trusted set"）
#: ⚠️ 不得把「能执行后续命令的包装器」放进来（env / nice / timeout / xargs / watch …），
#:    否则 `env rm -rf /` 会以 `env` 为准被误判为安全。包装器单独在 _WRAPPERS 里处理。
SAFE_READONLY = {
    "ls", "cat", "head", "tail", "less", "more", "wc", "file", "stat", "tree",
    "grep", "egrep", "fgrep", "find", "which", "whereis", "locate", "type",
    "date", "whoami", "pwd", "echo", "printf", "hostname", "uname", "id",
    "uptime", "printenv", "cal", "sleep", "true", "false", "tty", "groups",
    "users", "w", "last", "getent", "locale",
    "du", "df", "free", "ps", "top", "ss", "netstat", "lsof",
    "sort", "uniq", "cut", "tr", "nl", "diff", "cmp", "comm", "column",
    "paste", "join", "fold", "expand", "unexpand", "rev", "tac", "strings",
    "od", "xxd", "hexdump", "base64", "cksum", "seq", "expr", "bc", "jq",
    "awk", "gawk", "readlink", "realpath", "basename", "dirname",
    "md5sum", "sha1sum", "sha256sum", "sha512sum",
    "ping", "nslookup", "dig", "host", "whois",
}

#: ★ 包装器：它们本身无害，但会把**后面的东西当成命令来跑**。
#:   必须剥掉包装器、对剩余部分重新判定，否则就是一个绕过口子。
_WRAPPERS = {"env", "nice", "ionice", "nohup", "timeout", "stdbuf", "setsid",
             "time", "chrt", "doas", "sudo", "su", "exec", "command", "builtin"}

#: 会「对每一行执行一条命令」的，直接按需确认处理（无法静态展开）
_XARGS_LIKE = {"xargs", "watch", "parallel"}

#: 带 -i（就地修改）才算写操作的文本工具
_INPLACE_TOOLS = {"sed", "perl", "ruby", "ed"}

#: 只读的 git 子命令（有歧义的一律不放进来）
GIT_READONLY = {"status", "log", "diff", "show", "blame", "shortlog",
                "rev-parse", "ls-files", "describe", "whatchanged"}

DESTRUCTIVE_DELETE = {"rm", "rmdir", "shred", "truncate", "unlink"}
PERM_CHANGE = {"chmod", "chown", "chgrp", "chattr", "setfacl"}
ELEVATE = {"sudo", "su", "doas", "pkexec"}
PKG_WRITE = {"apt", "apt-get", "dpkg", "snap", "pip", "pip3", "npm", "yarn",
             "pnpm", "pacman", "dnf", "yum", "apk", "cargo", "gem"}
KILL = {"killall", "pkill", "kill", "xkill"}
POWER = {"reboot", "shutdown", "poweroff", "halt", "systemctl", "service"}
DISK = {"dd", "fdisk", "parted", "mkfs", "mount", "umount", "mkswap",
        "swapon", "swapoff", "blkid", "tune2fs", "e2fsck"}
NETWORK = {"curl", "wget", "ssh", "scp", "sftp", "rsync", "ftp", "telnet",
           "nc", "ncat", "socat"}

#: 受保护文件/目录（只读不许写）—— 照搬 Claude Code 的清单
PROTECTED_NAMES = {
    ".git", ".config/git", ".ssh", ".vscode", ".idea", ".husky", ".cargo",
    ".mvn", ".devcontainer",
    ".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".profile",
    ".zshrc", ".zprofile", ".zshenv", ".zlogin", ".zlogout", ".envrc",
    ".npmrc", ".yarnrc", ".gitconfig", ".gitmodules",
}

#: ★ 关键路径：删到这里就熔断（Claude Code 的 circuit breaker）
CRITICAL_PATHS = {
    "/", "/usr", "/etc", "/var", "/bin", "/sbin", "/lib", "/lib64", "/boot",
    "/dev", "/sys", "/proc", "/opt", "/srv", "/root", "/home", "/media",
    "/mnt", "/data", "/system", "/vendor", "/tmp",
}
_HOME_MARKS = {"~", "$home", "${home}", "$userprofile"}
_DOT = {".", "./", "..", "../"}

_WRITE_VERBS = DESTRUCTIVE_DELETE | PERM_CHANGE | {"mv", "cp", "tee", "sed",
                                                    "ln", "install", "truncate"}
_EDITORS = {"vim", "vi", "nano", "emacs", "gedit", "kate", "code"}


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _unquote(s: str) -> str:
    """去掉所有引号字符。`"$DIR"/*` ⇒ `$DIR/*`"""
    return s.replace('"', "").replace("'", "").strip()


def _first_word(segment: Sequence[Token]) -> str:
    for t in segment:
        if t.kind == "word":
            return t.text
    return ""


def _flags(words: Sequence[str]) -> List[str]:
    """取出所有选项（形如 -rf / --hard），保持原样。"""
    return [w for w in words[1:] if w.startswith("-") and w != "-"]


def _has_flag(words: Sequence[str], letters: str) -> bool:
    """短选项是否同时包含这些字母（-rf 对 "rf" 命中，-R 对 "R" 命中）。"""
    for f in _flags(words):
        body = f.lstrip("-")
        if f.startswith("--"):
            continue
        if body and all(ch.lower() in body.lower() for ch in letters):
            return True
    return False


def _operands(words: Sequence[str]) -> List[str]:
    """去掉命令名和选项后剩下的参数（就是操作对象）。"""
    return [w for w in words[1:] if not (w.startswith("-") and len(w) > 1)]


def _is_critical_target(raw: str) -> Optional[str]:
    """判断一个删除目标是否属于「关键路径」。命中返回人类可读的名字。"""
    t = _unquote(raw)
    if not t:
        return None
    norm = t.rstrip("/") or "/"
    if norm in CRITICAL_PATHS:
        return norm
    low = t.lower()
    if low in _HOME_MARKS:
        return "家目录（%s）" % t
    if low in _DOT:
        return "当前工作目录（%s）" % t
    # ★ 变量 + 通配：变量为空时等于从根删，Claude Code 也熔断这一类
    if re.match(r"^\$\{?\w+\}?/\*$", t) or re.match(r"^\$\{?\w+\}?/$", t):
        return "变量展开后的通配删除（变量为空时等于删根）：%s" % t
    if t == "/*" or re.match(r"^/\w+/\*$", t):
        return "根目录下的通配删除：%s" % t
    return None


def _protected_hit(words: Sequence[str]) -> Optional[str]:
    for w in words[1:]:
        raw = _unquote(w)
        base = raw.rstrip("/").split("/")[-1]
        for p in PROTECTED_NAMES:
            name = p.split("/")[-1]
            if raw.endswith(p) or base == name:
                return name
    return None


def _redirect_target(words: Sequence[str]) -> Optional[str]:
    """取出 `>` / `>>` 的重定向目标。"""
    joined = " ".join(words)
    m = re.search(r">>?\s*([^\s]+)", joined)
    if m:
        return _unquote(m.group(1))
    return None


def _has_redirect(words: Sequence[str]) -> bool:
    return bool(re.search(r">>?", " ".join(words)))


def _strip_wrapper(words: Sequence[str]) -> Optional[str]:
    """剥掉开头的包装器（env / nice / timeout / sudo …），返回剩余部分的命令串。

    这是堵住「`env rm -rf /` 被当成 `env` 放行」这类绕过口子的关键。
    剥不掉（只剩包装器本身）时返回 None。
    """
    w = list(words)
    while w and w[0] in _WRAPPERS:
        w = w[1:]
        # 跳过包装器自己的选项与它们的值（-n 19、-s TERM、--user root、纯数字的时长）
        while w and (w[0].startswith("-") or re.match(r"^\d+(?:\.\d+)?[smhd]?$", w[0])):
            flag = w[0]
            w = w[1:]
            if flag in ("-n", "-s", "-k", "-u", "--signal", "--kill-after", "--user"):
                if w and not w[0].startswith("-"):
                    w = w[1:]
    return " ".join(w) if w else None


def _inner_of_wrapper(words: Sequence[str]) -> Optional[Assessment]:
    """如果开头是包装器，递归判定被它包住的那条命令。"""
    if not words or words[0] not in _WRAPPERS:
        return None
    rest = _strip_wrapper(words)
    return assess(rest) if rest else None


# --------------------------------------------------------------------------- #
# 禁止档
# --------------------------------------------------------------------------- #
def _check_block(segment: Sequence[Token]) -> Optional[Assessment]:
    if not segment:
        return None
    words = shell_words(list(segment))
    cmd = words[0] if words else ""
    raw = "".join(t.text for t in segment)

    # BLOCK-01 关键路径删除
    if cmd in DESTRUCTIVE_DELETE:
        for w in _operands(words):
            hurt = _is_critical_target(w)
            if hurt:
                return Assessment(
                    BLOCK,
                    "这会删除「%s」—— 属于关键路径，删掉系统就没了，而且无法撤销。" % hurt,
                    "BLOCK-01", "关键路径删除", [hurt])

    # BLOCK-02 格式化设备 / 写裸设备
    if cmd == "mkfs" or cmd.startswith("mkfs"):
        return Assessment(BLOCK, "这条命令会格式化磁盘分区，分区上的数据会全部丢失。",
                          "BLOCK-02", "格式化设备", [raw])
    if cmd == "dd" and re.search(r"of=\s*/dev/(sd|hd|vd|nvme|block|mmcblk)", raw.replace(" ", "")):
        return Assessment(BLOCK, "这条命令会直接覆盖写入裸设备，等于毁掉整块磁盘。",
                          "BLOCK-02", "写裸设备", ["dd"])

    # BLOCK-03 fork 炸弹
    if re.search(r":\s*\(\s*\)\s*\{.*\|.*&.*\}", raw.replace(" ", "")) or \
       "(){:|:&};:" in raw.replace(" ", ""):
        return Assessment(BLOCK, "这是 fork 炸弹，会瞬间耗尽系统资源让机器死机。",
                          "BLOCK-03", "fork 炸弹", [])

    # BLOCK-04 全局放开权限
    if cmd in PERM_CHANGE and _has_flag(words, "R") and re.search(r"777|a\+rwx", raw):
        for w in _operands(words):
            if _unquote(w).rstrip("/") in ("", "/"):
                return Assessment(BLOCK, "把整个文件系统的权限开放成任何人可读写，系统会彻底失去保护。",
                                  "BLOCK-04", "全局放开权限", ["/"])

    # BLOCK-05 清空定时任务
    if cmd == "crontab" and _has_flag(words, "r"):
        return Assessment(BLOCK, "这会清空全部定时任务，没有回收站，删了就没了。",
                          "BLOCK-05", "清空定时任务", ["crontab -r"])

    # BLOCK-06 覆盖系统关键文件 / 裸设备
    tgt = _redirect_target(words)
    if tgt and tgt.startswith("/"):
        norm = tgt.rstrip("/")
        if re.match(r"^/etc/(passwd|shadow|gshadow|sudoers|fstab|hosts)$", norm) or \
           re.match(r"^/dev/(sd|hd|nvme|block|mmcblk)", norm) or norm == "/dev/sda":
            return Assessment(BLOCK, "这会直接覆盖系统关键文件「%s」，系统会立刻损坏。" % norm,
                              "BLOCK-06", "覆盖系统关键文件", [norm])

    # BLOCK-07 移动/删除根
    if cmd in ("mv", "cp") :
        if any(_unquote(w).rstrip("/") == "" for w in _operands(words)):
            return Assessment(BLOCK, "这会以根目录作为操作对象，系统会立刻崩溃。",
                              "BLOCK-07", "操作根目录", ["/"])

    return None


# --------------------------------------------------------------------------- #
# 需确认档
# --------------------------------------------------------------------------- #
_SIMPLE_CONFIRM = [
    (DESTRUCTIVE_DELETE, "CONFIRM-01", "破坏性删除", "会删掉文件，而且不进回收站，删了找不回来。"),
    (PERM_CHANGE,        "CONFIRM-02", "权限/属主变更", "会修改文件权限或归属，改错可能导致文件被锁死或被别人读到。"),
    (ELEVATE,            "CONFIRM-03", "提权执行", "会以管理员最高权限执行，越过普通用户的权限边界。"),
    (KILL,               "CONFIRM-04", "结束进程", "会强制结束进程，没来得及保存的数据会丢。"),
    (POWER,              "CONFIRM-05", "系统服务/电源操作", "会中断当前所有工作。"),
    (DISK,               "CONFIRM-06", "磁盘操作", "涉及磁盘或分区，操作失误容易毁数据。"),
]


def _check_confirm(segment: Sequence[Token]) -> Optional[Assessment]:
    if not segment:
        return None
    words = shell_words(list(segment))
    cmd = words[0] if words else ""
    raw = "".join(t.text for t in segment)

    # 动态执行（eval / source / sh -c）—— 内容静态看不出来，一律拦
    if cmd in DYNAMIC_EXEC:
        return Assessment(CONFIRM, "这条命令执行的是「动态生成的字符串」，我无法事先看出它到底要干什么。",
                          "CONFIRM-14", "动态执行", [cmd])
    if cmd in SHELLS and _has_flag(words, "c"):
        return Assessment(CONFIRM, "这是在另一个 shell 里执行一段字符串，我无法事先看出它的完整影响。",
                          "CONFIRM-14", "动态执行", [cmd])

    for verbs, rid, cat, why in _SIMPLE_CONFIRM:
        if cmd in verbs:
            return Assessment(CONFIRM, why, rid, cat, [cmd])

    # CONFIRM-08 包管理写操作（只读子命令除外）
    if cmd in PKG_WRITE:
        sub = words[1] if len(words) > 1 else ""
        readonly_ok = (
            (cmd in ("apt", "apt-get") and sub in ("list", "show", "search", "policy")) or
            (cmd == "dpkg" and (sub == "list" or "-l" in raw)) or
            (cmd in ("pip", "pip3") and sub in ("list", "show", "freeze")) or
            (cmd == "npm" and sub in ("list", "ls", "view", "outdated")) or
            (cmd == "yarn" and sub in ("list", "info")) or
            (cmd == "cargo" and sub in ("search", "tree")) or
            (cmd == "gem" and sub in ("list", "search"))
        )
        if not readonly_ok:
            return Assessment(CONFIRM, "这会安装、卸载或升级软件包，会改动系统环境。",
                              "CONFIRM-08", "包管理写操作", [cmd])

    # CONFIRM-09 破坏性 git
    if cmd == "git":
        sub = words[1] if len(words) > 1 else ""
        if sub == "push" and _has_flag(words, "f"):
            return Assessment(CONFIRM, "强制推送会覆盖远程仓库的提交，别人的工作可能被抹掉。",
                              "CONFIRM-09", "破坏性 git", ["git push --force"])
        if "--force" in words or "--force-with-lease" in words:
            return Assessment(CONFIRM, "强制操作会覆盖远程历史。", "CONFIRM-09", "破坏性 git", ["git push --force"])
        if sub == "reset" and "--hard" in words:
            return Assessment(CONFIRM, "硬重置会丢弃所有未提交的改动，且无法找回。",
                              "CONFIRM-09", "破坏性 git", ["git reset --hard"])
        if sub == "clean" and _has_flag(words, "f"):
            return Assessment(CONFIRM, "会删除未跟踪的文件，删掉就找不回来了。",
                              "CONFIRM-09", "破坏性 git", ["git clean"])
        if sub in ("branch", "tag") and (_has_flag(words, "d") or _has_flag(words, "D")):
            return Assessment(CONFIRM, "这会删除分支或标签。", "CONFIRM-09", "破坏性 git", ["git " + sub])
        if sub == "checkout" and "--" in words:
            return Assessment(CONFIRM, "会覆盖工作区文件，未提交的改动会丢。",
                              "CONFIRM-09", "破坏性 git", ["git checkout --"])
        if sub in ("push", "pull", "clone", "fetch", "remote"):
            return Assessment(CONFIRM, "这条 git 命令需要联网。", "CONFIRM-12", "需要联网", ["git " + sub])
        if sub == "config" and len(words) > 3:
            return Assessment(CONFIRM, "这会修改 git 配置。", "CONFIRM-15", "修改配置", ["git config"])

    # CONFIRM-10 修改受保护文件
    if cmd in (_WRITE_VERBS | _EDITORS):
        hit = _protected_hit(words)
        if hit:
            return Assessment(CONFIRM, "这会改动「%s」—— 它关系到登录环境或版本库，改错很难察觉。" % hit,
                              "CONFIRM-10", "受保护文件", [hit])

    # CONFIRM-11 覆盖写入
    if _has_redirect(words):
        tgt = _redirect_target(words) or "目标文件"
        if tgt != "/dev/null":
            return Assessment(CONFIRM, "这会用 `>` 覆盖写入「%s」，原有内容会被直接盖掉。" % tgt,
                              "CONFIRM-11", "覆盖写入", [tgt])

    # CONFIRM-12 需要联网
    if cmd in NETWORK:
        if not (cmd == "curl" and ("-I" in words or "--head" in words)):
            return Assessment(CONFIRM, "这条命令需要联网，会产生网络流量，也可能把本机内容发出去。",
                              "CONFIRM-12", "需要联网", [cmd])

    # CONFIRM-13 find 带删除/执行动作
    if cmd == "find":
        if "-delete" in words or "-delete" in raw:
            return Assessment(CONFIRM, "find 带 -delete 会真的删除匹配到的文件，不进回收站。",
                              "CONFIRM-13", "find 删除", ["-delete"])
        if "-exec" in words or "-exec" in raw:
            if re.search(r"-exec\s+(rm|shred|truncate|mv|chmod|chown)", raw):
                return Assessment(CONFIRM, "find 带 -exec 会对找到的每个文件执行删除/修改命令。",
                                  "CONFIRM-13", "find 执行动作", ["-exec"])
            return Assessment(CONFIRM, "find 带 -exec 会对找到的每个文件执行一条命令，请确认那条命令是安全的。",
                              "CONFIRM-13", "find 执行动作", ["-exec"])
        return None  # 纯查找 → 交给放行档

    # CONFIRM-17 逐行执行（xargs / watch / parallel）
    if cmd in _XARGS_LIKE:
        return Assessment(CONFIRM, "这条命令会把输入**逐行交给另一个命令去执行**，"
                                   "我无法事先看清它到底会跑什么。",
                          "CONFIRM-17", "逐行执行", [cmd])

    # CONFIRM-18 就地修改文件（sed -i / perl -i）
    if cmd in _INPLACE_TOOLS:
        if _has_flag(words, "i") or "--in-place" in words:
            return Assessment(CONFIRM, "这会**就地改写文件本身**，原文被直接覆盖。",
                              "CONFIRM-18", "就地修改文件", [cmd])
        return Assessment(ALLOW, "只读的文本处理，结果只打印到屏幕，不改动任何文件。",
                          "ALLOW-05", "只读文本处理")

    # CONFIRM-16 ip 命令的写操作
    if cmd == "ip":
        action = words[1] if len(words) > 1 else ""
        if action in ("addr", "a", "link", "route", "r", "neigh") and \
           not any(w in ("set", "add", "del", "delete", "up", "down") for w in words):
            return Assessment(ALLOW, "只读查询本机网络配置。", "ALLOW-03", "只读网络查询")
        return Assessment(CONFIRM, "这会修改本机网络配置，改错可能导致断网。",
                          "CONFIRM-16", "网络配置修改", ["ip " + action])

    return None


# --------------------------------------------------------------------------- #
# 放行档
# --------------------------------------------------------------------------- #
def _check_allow(segment: Sequence[Token]) -> Optional[Assessment]:
    if not segment:
        return None
    words = shell_words(list(segment))
    cmd = words[0] if words else ""

    if cmd == "git":
        sub = words[1] if len(words) > 1 else ""
        if sub in GIT_READONLY:
            return Assessment(ALLOW, "只读的 git 查询，不会改动任何东西。", "ALLOW-02", "只读 git")
        if sub in ("branch", "tag", "remote", "stash", "config") and len(words) == 2:
            return Assessment(ALLOW, "只是列出信息，不会改动任何东西。", "ALLOW-02", "只读 git")
        return None

    if cmd == "curl" and ("-I" in words or "--head" in words):
        return Assessment(ALLOW, "只要响应头，不下载内容。", "ALLOW-04", "只读网络查询")

    if cmd in SAFE_READONLY:
        return Assessment(ALLOW, "只读查询，看看而已，不会改动任何东西。", "ALLOW-01", "只读查询")

    return None


# --------------------------------------------------------------------------- #
# 递归处理 $() 与反引号
# --------------------------------------------------------------------------- #
_SUBST_DOLLAR = re.compile(r"\$\(([^()]*)\)")
_SUBST_TICK = re.compile(r"`([^`]*)`")


def _inner_commands(command: str, shell: Optional[str] = None) -> List[str]:
    """抽出「会被当命令执行」的内嵌内容。

    - `$( … )`：bash 与 PowerShell 都有，两平台都判。
    - 反引号：**只在 Linux/bash 下才代表"执行里面的命令"**。
      Windows 上（cmd 与 PowerShell）反引号是**转义符**，不是命令替换
      ⇒ 照搬过来会把 `dir `` `` ` 这种正常写法误判成"藏了命令"。

    不处理嵌套括号 —— 够用，且宁可少抽不要误抽。
    """
    sk = shell or active_shell()
    out: List[str] = [m.group(1) for m in _SUBST_DOLLAR.finditer(command)]
    if sk == SHELL_BASH:
        out += [m.group(1) for m in _SUBST_TICK.finditer(command)]
    return [s.strip() for s in out if s.strip()]


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def assess(command: str) -> Assessment:
    """对整条命令做风险判定，返回最严的一档。

    ★ 两套规则（Linux + Windows）都会跑，取最严 —— 见 `_win()` 里的说明。
    """
    if not command or not command.strip():
        return Assessment(CONFIRM, "空命令。", "CONFIRM-00", "无法判定")

    sk = active_shell()

    # 1) 先递归判定 $() 与反引号里的内容
    best: Optional[Assessment] = None
    for inner in _inner_commands(command, sk):
        a = assess(inner)
        best = a if best is None else _worse(best, a)
        if best.level == BLOCK:
            return best

    # 2) 再判定主命令
    segments = split_segments(tokenize(command), shell=sk)
    if not segments:
        return Assessment(CONFIRM, "没看懂这条命令，不敢替你下结论，请自己确认一遍。",
                          "CONFIRM-00", "无法判定")
    main = assess_segments(segments)
    return main if best is None else _worse(best, main)


def assess_segments(segments: Sequence[Sequence[Token]]) -> Assessment:
    """对多个子命令段分别判定，取最严的一档。

    这是 `ls && rm -rf /` 与 `dir & del /f /s /q C:\\` 都不会蒙混过关的原因
    —— 分段判定、取最严。
    """
    results: List[Assessment] = []
    for i, seg in enumerate(segments):
        words = shell_words(list(seg))
        raw = "".join(t.text for t in seg)

        # ★ 先剥包装器：
        #   Linux 侧 —— `env rm -rf /`、`sudo rm -rf /`、`timeout 5 rm -rf /`
        #   Windows 侧 —— `cmd /c del …`、`powershell -Command "Remove-Item …"`、`iex …`
        #   都必须以**被包住的那条命令**来判定，否则就是一个绕过口子。
        inner = _inner_of_wrapper(words)
        win_wrapped = False
        if inner is None and risk_win.is_win_shell_wrapper(words):
            rest = risk_win.win_wrapper_inner(words)
            inner = assess(rest) if rest else None
            win_wrapped = True

        # ★ 两套规则都跑：Linux 规则 → Windows 规则 → 需确认 → 放行，取最严
        a = (_check_block(seg) or _win(risk_win.check_block(words, raw))
             or _check_confirm(seg) or _win(risk_win.check_confirm(words, raw))
             or _check_allow(seg) or _win(risk_win.check_allow(words, raw)))
        if a is None:
            cmd = _first_word(seg) or "（未知）"
            a = Assessment(
                CONFIRM,
                "「%s」不在已知安全清单里，我判断不了它的影响，请你自己确认后再执行。" % cmd,
                "CONFIRM-99", "未知命令", [cmd])

        if words and words[0] in _WRAPPERS:
            if inner is None:
                # 光秃秃的包装器（`env`、`nice` 单独用）：只列信息，无害
                a = Assessment(ALLOW, "只是列出环境或进程信息，不改动任何东西。",
                               "ALLOW-06", "只读查询")
            elif words[0] in ELEVATE:
                # 提权包装器：对被包住的命令判定之外，再加一层提权确认
                a = _worse(a, inner)
            else:
                # 良性包装器（env / nice / timeout…）：以被包住的那条命令为准
                a = inner
        elif win_wrapped:
            # cmd / powershell / iex：外层本身是「动态执行」要确认，
            # 但真正决定档位的是它要执行的那段内容 ⇒ 取最严
            a = _worse(a, inner) if inner is not None else a

        results.append(a)
        if a.level == BLOCK:
            break

        # 跨段的危险组合：curl … | sh（把网上下载的脚本直接执行）
        if i + 1 < len(segments):
            left = _first_word(seg)
            right = _first_word(segments[i + 1])
            if left in NETWORK and right in RUNNERS:
                results.append(Assessment(
                    CONFIRM,
                    "这是把网上下载的内容直接交给 %s 执行 —— 执行的是别人写的代码，"
                    "你无法事先看清内容。" % right,
                    "CONFIRM-07", "远程执行管道", [left, right]))

    if not results:
        return Assessment(CONFIRM, "空命令。", "CONFIRM-00", "无法判定")

    worst = results[0]
    for r in results[1:]:
        worst = _worse(worst, r)
    if len(segments) > 1 and worst.level != ALLOW:
        worst.reason += "（这条命令由 %d 段组成，其中有一段是危险的）" % len(segments)
    return worst


_ICON = {ALLOW: "绿 · 可执行", CONFIRM: "红 · 需二次确认", BLOCK: "灰 · 已禁止"}


def explain(a: Assessment) -> str:
    """把判定结果渲染成命令行里给人看的文字。"""
    return "%s  %s\n    命中规则：%s %s" % (
        _ICON.get(a.level, a.level), a.reason,
        a.rule_id or "-", a.category or "-")


if __name__ == "__main__":  # pragma: no cover - 手动试跑
    import sys

    for c in (sys.argv[1:] or ["du -sh *", "rm -rf /"]):
        print("$ " + c)
        print(explain(assess(c)))
        print()
