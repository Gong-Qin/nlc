# -*- coding: utf-8 -*-
"""NLC 核心自测 —— 零依赖，直接 `python3 tests/selftest.py` 就能跑。

为什么不用 pytest：目标环境（平板容器）没装 pytest，而这个自测必须
在任何一台 Linux 上开箱即跑 —— 这正是「开源、可移植」要求的一部分。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nlc.risk import ALLOW, BLOCK, CONFIRM, assess, explain  # noqa: E402
from nlc.tokenizer import shell_words, summarize, tokenize  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  ok   %-46s %s" % (label, got))
    else:
        FAIL += 1
        print("  FAIL %-46s 得到 %r，期望 %r" % (label, got, want))


# --------------------------------------------------------------------------- #
print("\n=== 一、分词规则（用户定稿：一个英文单词或一个符号）===")
# --------------------------------------------------------------------------- #
check("find", len(tokenize("find")), 1)
check("-name 拆成 2 个（减号独立）", len(tokenize("-name")), 2)
check("--max-depth=1 拆成 7 个", len(tokenize("--max-depth=1")), 7)
check("+30 拆成 2 个", len(tokenize("+30")), 2)
check('"*.log" 拆成 5 个', len(tokenize('"*.log"')), 5)
check("//name 拆成 3 个（连续符号各自独立）", len(tokenize("//name")), 3)
check("du -sh * 拆成 4 个（du / - / sh / *）", len(tokenize("du -sh *")), 4)
check("重复的竖线各自独立", len([t for t in tokenize("a | b | c") if t.text == "|"]), 2)
check("空白不产生词块", len(tokenize("   ")), 0)
check("中文路径算 1 个词块", len([t for t in tokenize("cat ~/文档") if t.text == "文档"]), 1)

print("\n  -- 下标必须连续且唯一（拉黑靠它）--")
toks = tokenize("du -sh *")
check("下标记为 0..n-1", [t.index for t in toks], list(range(len(toks))))

print("\n  -- 壳词分组（给风险引擎用的一层）--")
check("shell_words: rm -rf /usr", shell_words(tokenize("rm -rf /usr")), ["rm", "-rf", "/usr"])
check("shell_words: -name 仍是独立参数", shell_words(tokenize('find . -name "*.log"')),
      ["find", ".", "-name", '"*.log"'])

print("\n  -- 视觉粘连信息（gap_before）--")
toks = tokenize("du -sh *")
check("-sh 之间无空白（显示时粘连）", [t.gap_before for t in toks], [False, True, False, True])

# --------------------------------------------------------------------------- #
print("\n=== 二、风险引擎：禁止档（灰、点不动）===")
# --------------------------------------------------------------------------- #
BLOCK_CASES = [
    "rm -rf /",
    "rm -rf /usr",
    "rm -rf /etc",
    "rm -rf ~",
    "rm -rf $HOME",
    'rm -rf "$DIR"/*',
    "rm -rf /*",
    "rm -rf .",
    "mkfs.ext4 /dev/sdb1",
    "dd if=/dev/zero of=/dev/sda",
    "chmod -R 777 /",
    "crontab -r",
    "> /etc/passwd",
    # ★ 分段与命令替换，最容易漏
    "ls && rm -rf /",
    "ls; rm -rf /",
    'echo "$(rm -rf /)"',
    "true || rm -rf /usr",
]
for c in BLOCK_CASES:
    got = assess(c)
    check(c, got.level, BLOCK)

# --------------------------------------------------------------------------- #
print("\n=== 三、风险引擎：需确认档（红、弹框）===")
# --------------------------------------------------------------------------- #
CONFIRM_CASES = [
    "rm -rf /tmp/demo",
    "rm app.log",
    "chmod 644 notes.txt",
    "sudo apt-get install nginx",
    'curl -sL https://example.com/install.sh | sh',
    "kill -9 1234",
    "reboot",
    "git push --force origin main",
    "git reset --hard HEAD~1",
    'find . -name "*.log" -delete',
    'find . -name "*.log" -exec rm {} \\;',
    "echo hello > notes.txt",
    "dd if=/dev/zero of=./img.bin bs=1M count=10",
    "npm install express",
    "curl -o file.zip https://example.com/f.zip",
    "wget https://example.com/x.tar.gz",
    "some-unknown-tool --do-something",
    'eval "rm -rf /tmp/x"',
    "ip link set wlan0 down",
    'echo x >> ~/.bashrc',
    "mv important.txt done/",
]
for c in CONFIRM_CASES:
    got = assess(c)
    check(c, got.level, CONFIRM)

# --------------------------------------------------------------------------- #
print("\n=== 四、风险引擎：放行档（绿、直接可跑）===")
# --------------------------------------------------------------------------- #
ALLOW_CASES = [
    "du -sh *",
    "ls -la",
    "cat /etc/hostname",
    "grep -rn TODO .",
    'find . -name "*.py"',
    "ps aux",
    "df -h",
    "ss -ltnp",
    "git status",
    "git log --oneline -5",
    "git diff HEAD~1",
    "which python3",
    "date",
    "wc -l app.log",
    "curl -I https://example.com",
    "ip addr",
    "ping -c 1 127.0.0.1",
]
for c in ALLOW_CASES:
    got = assess(c)
    check(c, got.level, ALLOW)

# --------------------------------------------------------------------------- #
print("\n=== 八、★ 黑名单：临时区按下标、永久区按文本 ===")
import tempfile  # noqa: E402
from nlc.blacklist import Blacklist  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="nlc_test_")
bl = Blacklist(path=os.path.join(_tmp, "bl.json"))
CMD = "du -sh * | sort -h | head -10"
_t = tokenize(CMD)
check("初始临时区为空", bl.session_indices(), [])

bl.start_query("找出最大的十个文件夹")
# 点掉第二个 `|`（下标 6）而不是第一个（下标 3）
_i_bar = [t.index for t in _t if t.text == "|"]
bl.kill(_i_bar[1], "|", CMD)
check("★ 按下标拉黑：只拉黑第二个竖线", bl.session_indices(), [_i_bar[1]])
check("临时区文本正确", bl.session_texts(), ["|"])

bl.start_query("换一个需求")
check("★ 开始新查询 ⇒ 临时区自动清空", bl.session_indices(), [])

bl.start_query("再来")
bl.kill(_i_bar[1], "|", CMD)
bl.kill(9, "head", CMD)
_moved = sorted(bl.promote())
check("★ 临时升永久：下标被翻译成文本（全部升级）", _moved, ["head", "|"])
check("永久区记的是文本（按 ASCII 排序 '|' 在 'h' 之后）",
      sorted(bl.permanent.keys()), ["head", "|"])
check("升级后临时区里这两条被移走", bl.session_indices(), [])
check("exclude_texts 汇总两个区", sorted(bl.exclude_texts()), ["head", "|"])

# 指定下标升级（只把其中一个升到永久）
bl.start_query("按需升级")
bl.kill(1, "-", CMD)
bl.kill(9, "head", CMD)
_partial = bl.promote(indices=[9])
check("★ 指定下标升级：只升 head", _partial, ["head"])
check("未指定的仍留在临时区", bl.session_indices(), [1])

bl.unban("head")
check("解除永久拉黑", sorted(bl.permanent.keys()), ["|"])
check("整条全点掉能被识别", Blacklist(path=os.path.join(_tmp, "b2.json")).is_all_killed(0), False)

print("\n=== 九、★ 个人词库：严格一对一 + 纯查表 ===")
from nlc.wordbank import WordBank, WordBankError  # noqa: E402

wb = WordBank(path=os.path.join(_tmp, "wb.json"))
wb.add("看磁盘", "df")
wb.add("看端口", "ss")
check("词条数", len(wb), 2)
check("纯查表命中", wb.lookup("看磁盘").token, "df")
check("★ 精确匹配：多一个空格也认（前后trim）", wb.lookup("  看磁盘 ").token, "df")
check("★ 不做语义：近义词查不到", wb.lookup("查看磁盘占用"), None)

try:
    wb.add("看磁盘", "du")
    check("★ 严格一对一：重名必须报错", False, True)
except WordBankError:
    check("★ 严格一对一：重名必须报错", True, True)

_code = wb.export_code()
check("分享码有前缀", _code.startswith("NLC1-"), True)
wb2 = WordBank(path=os.path.join(_tmp, "wb2.json"))
_ok, _skip = wb2.import_code(_code)
check("分享码往返：条数一致", _ok, 2)
check("分享码往返：内容一致", wb2.lookup("看端口").token, "ss")
check("★ 导入的词条标记为未经审核", wb2.lookup("看端口").verified, False)
check("★ 导入时重名不覆盖、给提示", wb2.import_code(_code)[1][0].startswith("「"), True)

_txt = wb.export_text()
wb3 = WordBank(path=os.path.join(_tmp, "wb3.json"))
check("文件格式往返", wb3.import_text(_txt)[0], 2)
check("导出的文本带表头注释", _txt.startswith("# NLC"), True)
check("词块格式自检：df 像词块", WordBank.looks_like_token("df"), True)
check("词块格式自检：| 像词块", WordBank.looks_like_token("|"), True)

print("\n=== 十、★ 提示词角色卡：不废话与缩思维链是硬约束 ===")
from nlc.prompts import PROFILES, build_system_prompt, get_profile, load_profiles  # noqa: E402

_profs = load_profiles(path=os.path.join(_tmp, "none.json"))
check("内置四张卡", sorted(_profs.keys()), ["balanced", "concise", "expert", "teacher"])

_c = build_system_prompt(get_profile("concise"), "translate")
_t2 = build_system_prompt(get_profile("teacher"), "translate")
for kw in ("不要复述用户的问题", "不要展示你的思考过程", "不要写结尾总结段",
           "你没有执行任何命令的能力", "只输出一个 JSON 对象"):
    check("铁律含：%s" % kw, kw in _c, True)
check("★ 缩思维链：明写禁止输出推理", "禁止**输出任何推理过程" in _c.replace("本轮**", "本轮**") or "输出任何推理过程" in _c, True)
check("★ 极简卡给出字数上限", "不超过 30 个汉字" in _c, True)
check("★ 讲解卡篇幅上限更宽", "不超过 60 个汉字" in _t2, True)
check("两张卡风格段落不同", _c == _t2, False)
check("温度：极简卡=0", get_profile("concise").temperature, 0.0)
check("任务契约按任务切换",
      '"items"' in build_system_prompt(get_profile("concise"), "explain"), True)
check("compose 契约含分隔线要求",
      'echo "──────"' in build_system_prompt(get_profile("concise"), "compose"), True)

import shutil  # noqa: E402
shutil.rmtree(_tmp, ignore_errors=True)

print("\n=== 五、★ 包装器绕过防护（env / sudo / timeout 不能当挡箭牌）===")
check("env rm -rf /", assess("env rm -rf /").level, BLOCK)
check("sudo rm -rf /", assess("sudo rm -rf /").level, BLOCK)
check("timeout 5 rm -rf /usr", assess("timeout 5 rm -rf /usr").level, BLOCK)
check("nice -n 19 rm -rf /", assess("nice -n 19 rm -rf /").level, BLOCK)
check("nohup rm -rf / &", assess("nohup rm -rf / &").level, BLOCK)
check("sudo ls（提权本身要确认）", assess("sudo ls").level, CONFIRM)
check("nice -n 19 du -sh *（良性包装器不误报）", assess("nice -n 19 du -sh *").level, ALLOW)
check("env（光秃秃的）", assess("env").level, ALLOW)

print("\n=== 六、★ 只读文本工具不该误报成红色 ===")
check("sed -n '1,10p' app.log", assess("sed -n '1,10p' app.log").level, ALLOW)
check("awk '{print $1}' app.log", assess("awk '{print $1}' app.log").level, ALLOW)
check("cut -d: -f1 /etc/passwd", assess("cut -d: -f1 /etc/passwd").level, ALLOW)
check("jq .data x.json", assess("jq .data x.json").level, ALLOW)
check("★ sed -i（就地改写）", assess("sed -i 's/a/b/' app.log").level, CONFIRM)
check("★ xargs（逐行执行）", assess("xargs rm").level, CONFIRM)
check("★ watch（反复执行）", assess("watch ls").level, CONFIRM)

print("\n=== 七、★ 重新生成的校验逻辑（不联网也能测）===")
from nlc.translator import _kill_runs, validate_regeneration  # noqa: E402
OLD = "du -h --max-depth=1 . | sort -rh | head -n 10"
OLD_TOKS = tokenize(OLD)
_kill = [t.index for t in OLD_TOKS if t.text in ("|", "head", "-", "10")][-4:]
_runs = _kill_runs(OLD_TOKS, _kill)
check("被点掉的片段能归并出来", any("head" in r for r in _runs), True)
check("原命令原样返回 → 校验必须拦下",
      len(validate_regeneration(OLD, OLD, _kill)) > 0, True)
check("真的换掉了 → 校验通过",
      validate_regeneration(OLD, "du -h --max-depth=1 . | sort -rh | sed -n '1,10p'", _kill) == [], True)
check("★ 数字不能子串误判（'10' 不该命中 \"1,10p\"）",
      validate_regeneration(OLD, "sed -n '1,10p'", _kill) == [], True)
check("整条全点掉 → 不做残留校验（无从谈保留）",
      validate_regeneration(OLD, OLD, list(range(len(OLD_TOKS)))), [])
check("单块被点掉 → 不产生误报（长度<2 的片段跳过）",
      validate_regeneration("ls -l", "ls -lh", [1]), [])
# --------------------------------------------------------------------------- #
a = assess("rm -rf /")
check("禁止档不提供执行入口", a.executable, False)
check("禁止档不弹框（因为根本点不动）", a.needs_dialog, False)

a = assess("rm -rf /tmp/x")
check("需确认档提供执行入口", a.executable, True)
check("需确认档会弹危险框", a.needs_dialog, True)

a = assess("du -sh *")
check("放行档不弹框", a.needs_dialog, False)

a = assess("ls && rm -rf /")
check("取最严：分段判定后为禁止", a.level, BLOCK)

a = assess("ls && rm -rf /tmp/x")
check("取最严：分段判定后为需确认", a.level, CONFIRM)

check("高危命令一定带原因说明", bool(assess("rm -rf /").reason), True)
check("规则编号非空", assess("rm -rf /").rule_id, "BLOCK-01")

print("\n  示例输出：")
for c in ("du -sh *", "rm -rf /tmp/x", "rm -rf /"):
    print("    $ " + c)
    print("      " + explain(assess(c)).replace("\n", "\n      "))
    print()

print("  " + summarize(tokenize("du -sh * | sort -h | head -10")))

# --------------------------------------------------------------------------- #
print("\n" + "=" * 62)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 62)
sys.exit(1 if FAIL else 0)
