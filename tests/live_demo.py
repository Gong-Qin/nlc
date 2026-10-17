# -*- coding: utf-8 -*-
"""NLC 翻译层实机演示（需要真实 API Key）。

跑法：python3 tests/live_demo.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nlc.providers import get_provider, load_config  # noqa: E402
from nlc.translator import compose, explain, lookup, regenerate, render_blocks, translate  # noqa: E402

_LEVEL = {"allow": "绿 · 可执行", "confirm": "红 · 需二次确认", "block": "灰 · 已禁止"}


def show(tr, killed=()):
    print("  命令   " + render_blocks(tr, killed))
    print("  词块   %d 个" % len(tr.blocks))
    if tr.summary:
        print("  说明   " + tr.summary)
    if tr.risk:
        print("  风险   %s —— %s" % (_LEVEL[tr.risk.level], tr.risk.reason))
        print("         命中规则 %s %s" % (tr.risk.rule_id, tr.risk.category))
    if tr.notes:
        print("  注意   " + tr.notes)
    print("  逐词块解释：")
    for b in tr.blocks:
        tag = "单词" if b.kind == "word" else "符号"
        glue = "" if b.gap_before else "（粘连）"
        print("    [%2d] %-8s %-4s%s  %s" % (b.index, b.text, tag, glue, b.meaning))
    print("  通道   %s" % tr.source)
    print()


def main() -> int:
    cfg = load_config()
    p = get_provider(cfg, "deepseek")
    ok, msg = None, None
    from nlc.providers import test as chan_test
    ok, msg = chan_test(p)
    print("通道体检：%s  %s\n" % ("通过" if ok else "不通", msg))
    if not ok:
        return 1

    print("═" * 70)
    print("① 查词 —— 一个词 → 一个命令词块")
    print("═" * 70)
    tr = lookup("查看端口占用", p)
    print("  查的词：查看端口占用")
    print("  结果   %s" % tr.command)
    print("  说明   %s" % tr.summary)
    print("  解释   %s" % tr.blocks[0].meaning)
    if tr.notes:
        print("  补充   %s" % tr.notes)
    print("  风险   %s" % _LEVEL[tr.risk.level])
    print()

    print("═" * 70)
    print("② 命令行 —— 一句话 → 命令 + 整句解释")
    print("═" * 70)
    print("  需求：找出占用空间最大的十个文件夹\n")
    tr2 = translate("找出占用空间最大的十个文件夹", p)
    show(tr2)

    print("═" * 70)
    print("③ 重新生成 —— 用户点掉了几个词块")
    print("═" * 70)
    # 模拟用户点掉最后四个词块（| head -10 那一段）
    kill = [b.index for b in tr2.blocks if b.text in ("|", "head", "-", "10")][-4:]
    if len(kill) < 2:
        kill = [b.index for b in tr2.blocks][-4:]
    print("  原命令：%s" % tr2.command)
    print("  点掉的词块：%s" % [tr2.blocks[i].text for i in kill if i < len(tr2.blocks)])
    print("  （其余部分应被保留）\n")
    tr3 = regenerate("找出占用空间最大的十个文件夹", tr2, kill, p)
    show(tr3, kill)

    print("═" * 70)
    print("④ 解释一条危险命令（本地风险引擎判定，不采信模型）")
    print("═" * 70)
    tr4 = explain('find . -name "*.log" -mtime +30 -delete', p)
    show(tr4)

    print("═" * 70)
    print("⑤ 对话式脚本 —— 把不相干的两条命令拼起来")
    print("═" * 70)
    tr5 = compose(["du -sh * | sort -h | head -5", "ss -ltnp"], p)
    print("  组合结果：")
    for line in tr5.command.splitlines():
        print("    " + line)
    print("  说明   %s" % tr5.summary)
    print("  风险   %s —— %s" % (_LEVEL[tr5.risk.level], tr5.risk.reason))
    if tr5.notes:
        print("  注意   %s" % tr5.notes)
    print()
    return 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())
