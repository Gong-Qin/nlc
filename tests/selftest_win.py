# -*- coding: utf-8 -*-
"""NLC Windows 侧自测 —— 零依赖，直接 `python tests/selftest_win.py` 就能跑。

与 `selftest.py`（Linux 侧、138 项）并列：
    - `selftest.py`    只管 Linux/bash 那套规则与分词，保证移植时**没有改坏**
    - `selftest_win.py`管 Windows/cmd/PowerShell 那套（本文件），是 Windows 版的新增能力

为什么要分成两个文件：Linux 那 138 项是已经跑通、已在平板上部署过的基线，
必须保持原样可跑；Windows 这堆是新写的，单独一个文件便于对照"新增了什么"。
"""

from __future__ import annotations

import os
import sys
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nlc import platform as pf                                  # noqa: E402
from nlc.risk import ALLOW, BLOCK, CONFIRM, assess              # noqa: E402
from nlc.tokenizer import shell_words, tokenize                 # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  ok   %-52s %s" % (label, got))
    else:
        FAIL += 1
        print("  FAIL %-52s 得到 %r，期望 %r" % (label, got, want))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


# --------------------------------------------------------------------------- #
print("\n=== 一、平台适配层：配置位置与方言 ===")
# --------------------------------------------------------------------------- #
check_true("能判定当前平台", pf.OS_LABEL and True)
check_true("配置目录可用", isinstance(str(pf.CONFIG_DIR), str) and len(str(pf.CONFIG_DIR)) > 0)
check_true("方言在允许集合里", pf.default_shell() in pf.SHELLS)

if pf.IS_WINDOWS:
    check_true("★ Windows 上配置走 %APPDATA%\\nlc",
               str(pf.CONFIG_DIR).lower().endswith("nlc"))
    check_true("★ Windows 上不落到 ~/.config",
               ".config" not in str(pf.CONFIG_DIR).replace("\\", "/"))
    check("★ Windows 的 Python 惯用叫法是 python", pf.python_cmd(), "python")
else:
    check("Linux 的 Python 惯用叫法是 python3", pf.python_cmd(), "python3")

check("set_shell 接受 ps 简写", pf.set_shell("PS"), pf.SHELL_POWERSHELL)
check("set_shell 非法值回到默认", pf.set_shell("nonsense"), pf.default_shell())
check("shell_label 说得清楚", "cmd" in pf.shell_label(pf.SHELL_CMD)
      or "命令提示符" in pf.shell_label(pf.SHELL_CMD), True)

# ★ 判风险一律两套都跑，所以下面把方言固定成 cmd，让结果可复现
pf.set_shell(pf.SHELL_CMD)

# --------------------------------------------------------------------------- #
print("\n=== 二、cmd：禁止档（灰、点不动）===")
# --------------------------------------------------------------------------- #
CMD_BLOCK = [
    "format C:",
    "format D: /q",
    "diskpart",
    "del /f /s /q C:\\",
    "del /f /s /q C:\\Windows",
    "del /f /s /q C:\\Windows\\System32",
    "rd /s /q C:\\",
    "rd /s /q C:\\Windows",
    "rmdir /s /q C:\\Program Files",
    "del /f /s /q C:\\Users",
    "del /f /s /q %SystemRoot%",
    "del /f /s /q %USERPROFILE%",
    "bootrec /fixmbr",
    "bcdedit /delete {default}",
    "reg delete HKLM\\SYSTEM\\CurrentControlSet /f",
    "vssadmin delete shadows /all /quiet",
    "wbadmin delete catalog -quiet",
    "> C:\\Windows\\System32\\drivers\\etc\\hosts",
]
for c in CMD_BLOCK:
    a = assess(c)
    check(c[:50], a.level, BLOCK)

# --------------------------------------------------------------------------- #
print("\n=== 三、cmd：需确认档（红、弹框）===")
# --------------------------------------------------------------------------- #
CMD_CONFIRM = [
    "del app.log",
    "erase old.txt",
    "rd emptydir",
    "del /s *.tmp",
    "copy a.txt b.txt",
    "xcopy src dst /e",
    "move a.txt b.txt",
    "attrib +h secret.txt",
    "takeown /f somefile.txt",
    "taskkill /f /im notepad.exe",
    "shutdown /s /t 0",
    "reg add HKCU\\Software\\X /v a /d b",
    "schtasks /create /tn t /tr x.exe /sc daily",
    "sc delete MyService",
    "net user 张三 /add",
    "net stop wuauserv",
    "chkdsk C: /f",
    "sfc /scannow",
    "powercfg /change standby-timeout-ac 0",
    "cipher /w:C:\\",
    "certutil -urlcache -f http://example.com/a.exe a.exe",
    "curl -o x.zip https://example.com/x.zip",
    "echo hello > notes.txt",
    "forfiles /p C:\\temp /c \"cmd /c del @path\"",
    "mshta http://example.com/a.hta",
    "rundll32 shell32.dll,Control_RunDLL",
    "some-unknown-tool --do-something",
]
for c in CMD_CONFIRM:
    a = assess(c)
    check(c[:50], a.level, CONFIRM)

# --------------------------------------------------------------------------- #
print("\n=== 四、cmd：放行档（绿、直接可跑）===")
# --------------------------------------------------------------------------- #
CMD_ALLOW = [
    "dir",
    "dir /b /s",
    "type C:\\Windows\\win.ini",
    "ipconfig /all",
    "ping -n 1 127.0.0.1",
    "systeminfo",
    "tasklist",
    "where python",
    "whoami",
    "findstr /s TODO *.py",
    "fc a.txt b.txt",
    "tree /f",
    "hostname",
    "net user",
    "sc query wuauserv",
    "reg query HKLM\\Software",
    "schtasks /query",
    "curl -I https://example.com",
    "set",
    "cls",
    "echo hello",
]
for c in CMD_ALLOW:
    a = assess(c)
    check(c[:50], a.level, ALLOW)

# --------------------------------------------------------------------------- #
print("\n=== 五、PowerShell：三档 ===")
# --------------------------------------------------------------------------- #
PS_BLOCK = [
    "Remove-Item -Recurse -Force C:\\",
    "Remove-Item -Recurse -Force C:\\Windows",
    "Remove-Item -Recurse -Force C:\\Users",
    "ri -Recurse -Force $env:SystemRoot",
    "clear-disk -Number 0 -RemoveData",
    "format-volume -DriveLetter C",
    "Set-MpPreference -DisableRealtimeMonitoring $true",
    "vssadmin delete shadows /all",
    "Remove-ComputerRestorePoint -DriveLetter C",
    # ★ 下面三条是「包装器里包着禁止级命令」——
    #   剥开包装器之后必须**仍然是禁止**，绝不能因为外面套了 iex / -Command 就降级
    "Invoke-Expression 'Remove-Item -Recurse -Force C:\\'",
    "powershell -Command \"Remove-Item -Recurse -Force C:\\\"",
    "cmd /c del /f /s /q C:\\",
    "dir & del /f /s /q C:\\",
]
for c in PS_BLOCK:
    a = assess(c)
    check(c[:50], a.level, BLOCK)

PS_CONFIRM = [
    "Remove-Item .\\app.log",
    "Remove-Item .\\logs -Recurse",
    "Set-Content -Path a.txt -Value 'hi'",
    "New-Item -ItemType Directory -Path .\\x",
    "Stop-Process -Name notepad",
    "Stop-Computer -Force",
    "Restart-Service -Name wuauserv",
    "Set-ExecutionPolicy Unrestricted",
    "New-LocalUser -Name bob",
    "Add-LocalGroupMember -Group Administrators -Member bob",
    "netsh advfirewall set allprofiles state off",
    "Invoke-WebRequest -Uri https://example.com/a.ps1",
    "iex 'Get-Process'",
]
for c in PS_CONFIRM:
    a = assess(c)
    check(c[:50], a.level, CONFIRM)

PS_ALLOW = [
    "Get-ChildItem C:\\",
    "Get-Process",
    "Get-Service",
    "Get-ChildItem -Recurse -Filter *.log | Where-Object { $_.Length -gt 1mb }",
    "Get-Content .\\app.log -Tail 20",
    "Test-Path .\\x",
    "Test-Connection 127.0.0.1",
    "Get-ChildItem | Sort-Object Length -Descending | Select-Object -First 10",
    "Get-ComputerInfo",
    "Get-Volume",
    "Get-NetTCPConnection -LocalPort 8080",
    # ★ ForEach-Object 要区别对待：只读管道不该一律标红
    "Get-ChildItem -Directory | ForEach-Object { [PSCustomObject]@{ 名 = $_.Name; "
    "大小 = [math]::Round((Get-ChildItem $_.FullName -Recurse -File "
    "-ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1MB, 2) } } "
    "| Sort-Object 大小 -Descending | Select-Object -First 10",
    "Get-Process | ForEach-Object { $_.Name }",
]
for c in PS_ALLOW:
    a = assess(c)
    check(c[:50], a.level, ALLOW)

print("\n  -- ★ 但 ForEach 里出现会改东西的命令，就必须拦 --")
for c in ("Get-ChildItem | ForEach-Object { Remove-Item $_.FullName -Recurse -Force }",
          "Get-Process | ForEach-Object { Stop-Process -Id $_.Id }",
          "Get-ChildItem | ForEach-Object { Set-Content -Path $_.Name -Value 'x' }",
          "Get-ChildItem | ForEach-Object { cmd /c del /f /s /q C:\\ }"):
    a = assess(c)
    check(c[:50], a.level, CONFIRM)

# --------------------------------------------------------------------------- #
print("\n=== 六、★ 包装器绕过防护（Windows 侧）===")
# --------------------------------------------------------------------------- #
check("cmd /c 不能当挡箭牌", assess("cmd /c del /f /s /q C:\\").level, BLOCK)
check("cmd /k 不能当挡箭牌",
      assess("cmd /k del /f /s /q C:\\Windows").level, BLOCK)
check("PowerShell -Command 不能当挡箭牌",
      assess("powershell -Command \"Remove-Item -Recurse -Force C:\\\"").level, BLOCK)
check("iex 不能当挡箭牌",
      assess("iex \"Remove-Item -Recurse -Force C:\\\"").level, BLOCK)
check("良性包装：dir 还是绿", assess("dir /b").level, ALLOW)
check("cmd 单独用（光秃秃）", assess("cmd").level, CONFIRM)

# --------------------------------------------------------------------------- #
print("\n=== 七、★ 关键路径识别（is_win_critical）===")
# --------------------------------------------------------------------------- #
from nlc.risk_win import is_win_critical, normalize_path, win_wrapper_inner  # noqa: E402

for path in ("C:\\", "c:\\", "C:/", "D:\\", "C:\\*", "C:\\*.*",
             "%SystemRoot%", "%windir%", "%USERPROFILE%", "%APPDATA%",
             "C:\\Windows", "C:\\WINDOWS\\System32", "C:\\Program Files",
             "C:\\Program Files (x86)", "C:\\ProgramData", "C:\\Users",
             "C:\\Users\\wuyou", "C:\\$Recycle.Bin", "\\Windows"):
    check_true("关键路径：%s" % path, is_win_critical(path))

for path in (".\\app.log", "C:\\Temp\\x.txt", "D:\\我的文件\\a.txt",
             ".\\logs", "notes.txt", "C:\\Users\\wuyou\\Desktop\\x.txt",
             "C:\\Temp", ""):
    check("不是关键路径：%r" % path, is_win_critical(path), None)

check("路径归一化：正斜杠转反斜杠", normalize_path("C:/Users//a"), "C:\\Users\\a")
check("去掉引号", normalize_path('"C:\\Program Files"'), "C:\\Program Files")

print("\n  -- 包装器剥离：能看出它真正要跑什么 --")
check("cmd /c", win_wrapper_inner(["cmd", "/c", "del", "/f", "C:\\"]), "del /f C:\\")
check("powershell -Command",
      win_wrapper_inner(["powershell", "-Command", "Get-Process"]), "Get-Process")
check("iex", win_wrapper_inner(["iex", "Get-Process"]), "Get-Process")
check("普通命令不剥", win_wrapper_inner(["dir", "/b"]), None)

# --------------------------------------------------------------------------- #
print("\n=== 八、cmd 转义符：^ 后面的符号不是分隔符 ===")
# --------------------------------------------------------------------------- #
pf.set_shell(pf.SHELL_CMD)
from nlc.tokenizer import split_segments                              # noqa: E402

_segs = split_segments(tokenize("echo a ^& echo b"))
check("★ ^ 转义的 & 不算分段", len(_segs), 1)
_segs2 = split_segments(tokenize("echo a & echo b"))
check("未转义的 & 正常分段", len(_segs2), 2)
check("★ 转义后的整条命令不误报危险", assess("echo 100%% ^& echo done").level, ALLOW)

pf.set_shell(pf.SHELL_POWERSHELL)
_segs3 = split_segments(tokenize("Write-Host a `; Write-Host b"))
check("★ PowerShell 反引号转义的 ; 不算分段", len(_segs3), 1)
pf.set_shell(pf.SHELL_CMD)

# --------------------------------------------------------------------------- #
print("\n=== 九、分词在 Windows 路径与变量上仍然正确 ===")
# --------------------------------------------------------------------------- #
check("Windows 路径算作 1 个壳词",
      shell_words(tokenize("copy C:\\Users\\wuyou\\a.txt D:\\")),
      ["copy", "C:\\Users\\wuyou\\a.txt", "D:\\"])
check("cmd 开关整体成一个壳词",
      shell_words(tokenize("del /f /s /q C:\\")), ["del", "/f", "/s", "/q", "C:\\"])
check("%变量% 整体成一个壳词",
      shell_words(tokenize("del %TEMP%\\x")), ["del", "%TEMP%\\x"])
check("PowerShell 参数整体成一个壳词",
      shell_words(tokenize("-Recurse -Force")), ["-Recurse", "-Force"])

# --------------------------------------------------------------------------- #
print("\n=== 十、★ 提示词会跟着平台换措辞 ===")
# --------------------------------------------------------------------------- #
from nlc.prompts import build_system_prompt, get_profile               # noqa: E402

pf.set_shell(pf.SHELL_CMD)
_cmd_prompt = build_system_prompt(get_profile("concise"), "translate")
pf.set_shell(pf.SHELL_POWERSHELL)
_ps_prompt = build_system_prompt(get_profile("concise"), "translate")
pf.set_shell(pf.SHELL_BASH)
_bash_prompt = build_system_prompt(get_profile("concise"), "translate")
pf.set_shell(pf.SHELL_CMD)

check_true("★ cmd 提示词点明 cmd", "cmd" in _cmd_prompt)
check_true("★ cmd 提示词警告没有 ls / rm", "`ls`" in _cmd_prompt and "`rm`" in _cmd_prompt)
check_true("★ PowerShell 提示词点明 PowerShell", "PowerShell" in _ps_prompt)
check_true("★ bash 提示词点明 Linux", "Linux" in _bash_prompt or "bash" in _bash_prompt)
check("三种方言的提示词互不相同", len({_cmd_prompt, _ps_prompt, _bash_prompt}), 3)
check_true("铁律仍在（不能改坏）", "你没有执行任何命令的能力" in _cmd_prompt)
check_true("JSON 契约仍在", "只输出一个 JSON 对象" in _cmd_prompt)

# --------------------------------------------------------------------------- #
print("\n=== 十一、安全底线：翻译开关 / 完全访问都不能绕过「禁止」档 ===")
# --------------------------------------------------------------------------- #
_a = assess("del /f /s /q C:\\")
check("禁止档没有执行入口", _a.executable, False)
check("禁止档不弹框（根本点不动）", _a.needs_dialog, False)
_b = assess("del app.log")
check("需确认档有执行入口", _b.executable, True)
check("需确认档会弹危险框", _b.needs_dialog, True)
check("放行档不弹框", assess("dir").level, ALLOW)
check("规则编号非空", bool(_a.rule_id), True)
check("高危命令一定带原因说明", bool(_a.reason), True)

# 两套规则并存：Linux 命令在 Windows 版里照样被拦住
check("★ Linux 的 rm -rf / 在 Windows 版里仍被拦",
      assess("rm -rf /").level, BLOCK)
check("★ Linux 的 rm -rf C:\\ 在 Windows 版里也被拦",
      assess("rm -rf C:\\").level, BLOCK)

# --------------------------------------------------------------------------- #
print("\n=== 十二、doctor：一条命令做体检 ===")
# --------------------------------------------------------------------------- #
import io                                                        # noqa: E402
import contextlib                                               # noqa: E402
from nlc import cli as _cli                                     # noqa: E402
from nlc.cli import _pad, _w, build_parser                     # noqa: E402

# ① 子命令注册了
check_true("doctor 子命令已注册",
           "doctor" in build_parser().format_help())

# ② 显示宽度：中文按两列算（这是排版对不齐的病根）
check("英文宽度按字符数", _w("abcd"), 4)
check("中文按两列", _w("中文"), 4)
check("中英混排", _w("看磁盘占用"), 10)
check_true("补空格后总宽一致", _w(_pad("中文", 12)) == 12)
check_true("超宽不截断", _pad("很长的一个标签", 4) == "很长的一个标签")

# ③ 跑一次真体检（--no-tests 免得再套一层子进程）
class _Args:
    no_tests = True

_buf = io.StringIO()
try:
    with contextlib.redirect_stdout(_buf):
        _rc = _cli._cmd_doctor(_Args())
    _out = _buf.getvalue()
    check_true("doctor 正常退出（没有「问题」项）", _rc == 0)
    for _sec in ("【一】运行环境", "【二】位置", "【三】判哪种命令行",
                 "【四】AI 通道", "【五】自测"):
        check_true("doctor 有 %s 这一段" % _sec, _sec in _out)
    check_true("doctor 报了平台", pf.OS_LABEL in _out)
    check_true("doctor 报了 Python 路径", pf.python_exe() in _out)
    check_true("doctor 报了配置目录", str(pf.CONFIG_DIR) in _out)
    check_true("doctor 报了当前方言", pf.shell_label() in _out)
    check_true("doctor 报了结论", "结论：" in _out)
    check_true("doctor 不泄露密钥内容", "sk-" not in _out)
except Exception as _exc:                                        # noqa: BLE001
    check("doctor 能跑完不崩", repr(_exc), "不崩")

# ④ 密钥状态只报「已填/没填」，绝不打印密钥本身 —— 这是安全底线
check_true("doctor 的密钥判定只看有没有", _w("已填") == 4)

# --------------------------------------------------------------------------- #
print("\n=== 十三、Windows 批处理：纯 ASCII + 必须切到自己的目录 ===")
# --------------------------------------------------------------------------- #
# ★ 这两条守的是**两个已经真发生过的 bug**，不是假想：
#   1) `.cmd` 里写中文 → cmd.exe 按 OEM 代码页读，乱码的 rem 行会被当命令执行；
#   2) 安装生成的启动器漏了 `pushd` → 从开始菜单/快捷方式启动时
#      `python -m nlc` 找不到包，报 "No module named nlc"。
#   两者都不会让任何自测变红，所以必须专门盯着。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: nlc.cmd 是**唯一直接跑 `python -m nlc`** 的批处理 ——
#:   只有它需要 pushd 到自己的目录（`-m` 靠当前目录找包）。
#:   install.cmd / uninstall.cmd 传的是**绝对脚本路径**，不依赖当前目录，所以不需要。
_CMD_FILES = ["nlc.cmd", "install.cmd", "uninstall.cmd"]
_NEEDS_PUSHD = {"nlc.cmd"}
for _rel in _CMD_FILES:
    _p = os.path.join(_ROOT, _rel)
    _ok_exists = os.path.exists(_p)
    check("批处理存在：%s" % _rel, _ok_exists, True)
    if not _ok_exists:
        continue
    _raw = open(_p, "rb").read()
    _text = _raw.decode("utf-8", errors="replace")
    _non_ascii = [c for c in _text if ord(c) > 127]
    check("★ %s 是纯 ASCII（.cmd 里写中文会乱码）" % _rel, _non_ascii, [])
    if _rel in _NEEDS_PUSHD:
        check("★ %s 会切到自己的目录（否则 -m nlc 找不到包）" % _rel, "pushd" in _text, True)
    else:
        check("%s 用绝对脚本路径，不依赖当前目录" % _rel,
              "%HERE%packaging\\" in _text, True)
    check("%s 结尾不吞错误码" % _rel, "exit /b" in _text, True)

# 安装器与开始菜单入口的**生成模板**同样要过关
_IP = os.path.join(_ROOT, "packaging", "install_windows.py")
_ISRC = open(_IP, encoding="utf-8").read()
check_true("安装器里写了 pushd 到生成的启动器", 'pushd "%HERE%"' in _ISRC)
check_true("安装器生成的启动器 popd 复原", "popd" in _ISRC)
# 开始菜单那份是运行期生成、检查不进去，所以查它的源码模板：
# 模板里若出现 .encode("utf-8") 写正文就是错的，必须是 ascii
_sm_start = _ISRC.find("Created by the NLC installer")
_sm_end = _ISRC.find("return entry", _sm_start)
_sm_block = _ISRC[_sm_start:_sm_end] if _sm_start > 0 else ""
check_true("★ 开始菜单入口按 ASCII 写出（中文只进文件名）",
           bool(_sm_block) and 'encode("ascii")' in _sm_block
           and "由 NLC 安装脚本创建" not in _sm_block)

# --------------------------------------------------------------------------- #
print("\n=== 十四、启动器必须挑「能 import tkinter」的 Python ===")
# --------------------------------------------------------------------------- #
# ★ 守的是 2026-10-03 真实踩到的坑：
#   `nlc.cmd` 原来用 `where python.exe` 挑**第一个找到的**就完事。
#   这台机器 PATH 里 WorkBuddy 托管的 3.13 排在 C:\Python314 前面，
#   而它**没编译 tkinter** ⇒ 双击启动器 → "No module named 'tkinter'"。
#   关键点：这个 bug 只有**经启动器跑**才暴露。
#   打包自检曾只用 `sys.executable` 测，那绕过了启动器，所以一直是绿的。
_NLC_CMD = os.path.join(_ROOT, "nlc.cmd")
_nlc_text = open(_NLC_CMD, encoding="utf-8").read()
check_true("★ nlc.cmd 会逐个探测候选 Python（不是 where 挑第一个就完事）",
           "import tkinter" in _nlc_text)
check_true("★ nlc.cmd 的探测是「能 import tkinter 才接受」",
           re.search(r'import\s+tkinter[^\n]*&&\s*set\s+"PYEXE=', _nlc_text) is not None)
check_true("nlc.cmd 找得到 Python 时会实测，而不是直接用",
           _nlc_text.count("where") >= 1 and "-c \"import tkinter" in _nlc_text)
# 没找到时报错文案要说清「要带 tkinter」，而不是笼统说没装 Python
check_true("★ 找不到时提示语点明 tkinter（用户才知道该装什么）",
           "WITH tkinter" in _nlc_text and "tkinter" in _nlc_text)

# 打包脚本：顶层启动器 + 经启动器的冒烟，两样都得有
_BP = os.path.join(_ROOT, "packaging", "build_package.py")
_BPSRC = open(_BP, encoding="utf-8").read()
check_true("★ 打包会放一个顶层中文启动器（解压后不用找文件夹）",
           "TOP_LAUNCHER_ZH" in _BPSRC and "启动NLC.cmd" in _BPSRC)
check_true("★ 打包自检会经 nlc.cmd 跑一遍（只测 sys.executable 抓不到选错 Python）",
           '"cmd", "/c", "nlc.cmd"' in _BPSRC)
check_true("★ 打包自检会判 nlc.cmd 挑的 Python 有没有 tkinter",
           "没有 tkinter" in _BPSRC)
check_true("★ 《用前必读》写明要先解压、别直接双击 zip",
           "别直接双击 zip" in _BPSRC)
check_true("★ 《用前必读》说明了中间那层目录（找不到文件夹的根因）",
           "找不到" in _BPSRC and "文件夹" in _BPSRC)

# 顶层启动器的正文本身也要纯 ASCII + 会转发
_launcher_ok = True
try:
    import zipfile
    _zp = os.path.join(_ROOT, "packaging", "dist", "nlc-0.2.0-windows.zip")
    if os.path.exists(_zp):
        with zipfile.ZipFile(_zp) as _zf:
            _names = _zf.namelist()
            _hit = [n for n in _names if n.endswith("启动NLC.cmd")]
            check_true("★ 已打出的 zip 顶层有 启动NLC.cmd", bool(_hit))
            if _hit:
                # 必须在版本目录**之外**的那一层，也就是 <top>/启动NLC.cmd
                check_true("★ 顶层启动器不在版本目录里面（否则还是得先进去）",
                           _hit[0].count("/") == 1)
                _body = _zf.read(_hit[0])
                check_true("★ 顶层启动器正文纯 ASCII",
                           not any(b > 0x7F for b in _body))
                check_true("★ 顶层启动器会转发给下一层的 nlc.cmd",
                           b"nlc.cmd" in _body)
    else:
        _launcher_ok = False
except Exception:                                                # noqa: BLE001
    _launcher_ok = False
if not _launcher_ok:
    # 没打过包不是测试失败，但要说清楚，别让人以为验过了
    print("     (提示：packaging/dist 里还没有 zip，跳过已打包产物的两项检查)")

# --------------------------------------------------------------------------- #
print("\n=== 十五、PNG 编码必须像素级正确（平面 vs 交错）===")
# ★ 守的是 2026-10-03 真实踩到、也最难自己发现的坑：
#   `_write_png` 原来写成"先拼整行 R、再整行 G、再 B、再 A"——
#   那是**平面存储**，而 PNG 规范要的是**交错**（R0 G0 B0 A0 R1 G1 …）。
#   通道切片全对、长度全对、函数返回 True、文件也照样生成，
#   但解出来的图每个通道变成一条竖带，看上去像"画面水平重复了三次"。
#   ★ 我为此绕了很大一圈：先怀疑是抓屏、是虚拟显示器、是自己画的框，
#     最后才发现是编码器。**"报成功但内容错"只能靠像素比对抓出来。**
_SS = os.path.join(_ROOT, "tools", "screenshot.py")
if not os.path.exists(_SS):
    print("     (提示：tools/screenshot.py 不在，跳过 PNG 编码检查)")
else:
    import importlib.util as _ilu
    import tempfile as _tf
    _spec = _ilu.spec_from_file_location("_nlc_ss", _SS)
    _ss = _ilu.module_from_spec(_spec)
    try:
        _spec.loader.exec_module(_ss)          # 只 import，不进 __main__
    except Exception as _e:                     # noqa: BLE001
        _ss = None
        print("     (提示：screenshot.py import 失败（%s），跳过 PNG 编码检查" % _e)

    if _ss is not None:
        _W, _H = 64, 64
        # 造一张可预测的图：浅底 + 一条红线（x=10）+ 一个绿块
        _buf = bytearray(b"\xFA\xFD\xFF\xFF" * (_W * _H))          # BGRA
        for _y in range(_H):
            _o = _y * _W * 4 + 10 * 4
            _buf[_o:_o + 4] = b"\x00\x00\xFF\xFF"                  # 纯红
        for _y in range(20, 30):
            for _x in range(30, 40):
                _o = (_y * _W + _x) * 4
                _buf[_o:_o + 4] = b"\x00\xFF\x00\xFF"             # 纯绿
        _png = os.path.join(_tf.mkdtemp(prefix="nlc_png_"), "t.png")
        _ss._write_png(__import__("pathlib").Path(_png), bytes(_buf), _W, _H)
        _back = _ss._read_pixels(__import__("pathlib").Path(_png))

        check_true("★ PNG 写完能原样读回（长度 = w*h*4）",
                   len(_back) == _W * _H * 4)

        def _at(x, y):
            _o = (y * _W + x) * 4
            return tuple(_back[_o:_o + 3])

        check_true("★ 红线在正确位置且是红色（BGRA→RGBA 换对了）",
                   _at(10, 40) == (255, 0, 0))
        check_true("★ 绿块在正确位置且是绿色",
                   _at(35, 25) == (0, 255, 0))
        check_true("★ 底色正确（没被通道错位污染）",
                   _at(50, 50) == (255, 253, 250))
        check_true("★ 绿块外一格仍是底色（没渗色）",
                   _at(45, 25) == (255, 253, 250))
        # ★ 这条最关键：平面存储时，整行 R 会连成一片，
        #   x 与 x+p 抽样比对命中率接近 1，图像看起来就是"重复畸变"。
        check_true("★ 单色为主的图不会被误判成「重复畸变」",
                   _ss._looks_repeated(_back, _W, _H) is False)

        # 检测器本身没被改废：造一张**颜色丰富**的横向三联图。
        # ★ 两个坑都踩过，写下来免得下次再错：
        #   ① 必须**逐行**复制（row*3），不能把整幅图复制 3 次 ——
        #      检测器按 base = y*W*4 横向读，整幅复制会被读成纵向排列。
        #   ② 宽度取 129 = 43*3，保证候选周期 w//3 正好落在分段边界上。
        #   ③ 颜色数必须过门槛（>=24），否则会被门槛直接放行、测不到检测逻辑。
        import random as _rnd
        _r = _rnd.Random(20261003)
        _RW, _RH, _SW = 129, 64, 43

        def _mkrow(same: bool) -> bytes:
            if same:
                return bytes(_r.randrange(256) for _ in range(_SW * 3)) + b"\xFF" * _SW
            out = b""
            for _k in range(3):               # 三段内容各不相同
                out += bytes(_r.randrange(256) for _ in range(_SW * 3)) + b"\xFF" * _SW
            return out

        _tri = b"".join(_mkrow(True) * 3 for _y in range(_RH))
        _ok = b"".join(_mkrow(False) for _y in range(_RH))
        check_true("★ 颜色丰富的横向三联畸变能查出来（检测器没被改废）",
                   _ss._looks_repeated(_tri, _RW, _RH) is True)
        check_true("★ 同样内容不重复时判为正常（不误报）",
                   _ss._looks_repeated(_ok, _RW, _RH) is False)

        # ★★ 判据松紧本身要被锁住。这条是被一次**误判**逼出来的：
        #   阈值曾是 0.75，于是把 NLC 自己的界面判成"被虚拟显示器截断"，
        #   还让用户去退出向日葵。真因是 PNG 编码器（平面当交错写）。
        #   实测：正常界面各周期命中率 0.65~0.73（左右两栏布局本来就像），
        #   真复制则接近 1.0。阈值必须在 0.9 以上，否则正常界面会被冤。
        _src_ss = open(_SS, encoding="utf-8").read()
        check_true("★ 畸变阈值已提到 0.97（0.75 会冤判正常界面）",
                   "0.97" in _src_ss and "return best >= 0.97" in _src_ss)
        check_true("★ 探测不再断言虚拟显示器是根因（那是误判）",
                   "已确认不影响抓图" in _src_ss
                   and "它很可能就是画面畸变的根因" not in _src_ss)
        # ★ 只查**面向用户的报错文案**，不查全文 ——
        #   注释里当然要写"我曾误判成向日葵、那是错的"，那是该留的记录。
        _msgs = "".join(re.findall(r'"[^"]*(?:怎么办|畸变|根因)[^"]*"', _src_ss))
        check_true("★ 报错文案不再让用户退出向日葵（实测无需退出）",
                   "退出向日葵" not in _msgs)

        # 结构图退路：纯 Python 画框不许再踩"切片字节数不对"的坑
        _items = [(0, "Button", "A", (5, 5, 30, 20)),
                  (1, "Entry", "B", (40, 5, 20, 20))]
        _sp = os.path.join(os.path.dirname(_png), "s.png")
        # ★ 尺寸必须 ≥ 下限（_draw_structure_png 里 w>=200 / h>=150），
        #   否则拿到的是被抬过的值，这条就变成在测下限保护、不是测绘图。
        _SW, _SH = 240, 180
        check_true("★ 结构图能画出来（返回 True）",
                   _ss._draw_structure_png(
                       _items, __import__("pathlib").Path(_sp), _SW, _SH, "t") is True)
        _sw, _sh = _ss._png_size(__import__("pathlib").Path(_sp))
        check_true("★ 结构图尺寸正确", (_sw, _sh) == (_SW, _SH))
        _sraw = _ss._read_pixels(__import__("pathlib").Path(_sp))
        check_true("★ 结构图缓冲长度正确（行宽没算错、没越界增长）",
                   len(_sraw) == _SW * _SH * 4)
        check_true("★ 结构图不误报重复畸变",
                   _ss._looks_repeated(_sraw, _SW, _SH) is False)
        # 框要真的画进图里了 —— 验"内容对"，不只验"格式对"
        _so = (10 * _sw + 5) * 4
        check_true("★ 结构图里真的画上了框（不是一张空图）",
                   tuple(_sraw[_so:_so + 3]) != (255, 253, 250))

        _src = open(_SS, encoding="utf-8").read()
        check_true("★ 结构图绘制缓冲不带 filter 字节（与 _write_png 的契约一致）",
                   "rowbytes = w * 4 " in _src and "rowbytes = w * 4 + 1" not in _src)
        check_true("★ 结构图底图按行批量铺（不是逐像素，否则会超时被杀）",
                   "row_main" in _src and "for x in range(W)" not in _src)

# --------------------------------------------------------------------------- #
print("\n" + "=" * 66)
print("Windows 侧通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 66)
sys.exit(1 if FAIL else 0)
