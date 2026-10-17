# -*- coding: utf-8 -*-
"""角色卡效果对比：同一句话，用不同角色卡翻译，看篇幅与信息量的差别。

跑法：python3 tests/profile_compare.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 英文 Windows 的控制台默认 cp1252，打印中文会 UnicodeEncodeError —— 统一强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from nlc.prompts import get_profile, load_profiles  # noqa: E402
from nlc.providers import get_provider, load_config, test  # noqa: E402
from nlc.translator import translate  # noqa: E402

QUESTION = "找出当前目录下占用空间最大的十个文件夹"
CARDS = ["concise", "balanced", "teacher", "expert"]


def main() -> int:
    cfg = load_config()
    p = get_provider(cfg, "deepseek")
    ok, msg = test(p)
    if not ok:
        print("通道不通：%s" % msg)
        return 1

    profs = load_profiles()
    print("问题：%s\n" % QUESTION)
    print("=" * 74)

    for cid in CARDS:
        prof = profs[cid]
        tr = translate(QUESTION, p, profile=prof)
        summary_len = len(tr.summary or "")
        meaning_lens = [len(b.meaning) for b in tr.blocks]
        avg = (sum(meaning_lens) // len(meaning_lens)) if meaning_lens else 0
        total = summary_len + sum(meaning_lens) + len(tr.notes or "")

        print("\n【%s %s】（温度 %.1f，上限 %d 字）" % (prof.emoji, prof.name, prof.temperature, prof.max_tokens))
        print("  命令      %s" % tr.command)
        print("  说明(%2d字) %s" % (summary_len, tr.summary))
        print("  词块      %d 个，单块解释平均 %d 字" % (len(tr.blocks), avg))
        print("  注意(%2d字) %s" % (len(tr.notes or ""), (tr.notes or "（空）")[:80]))
        print("  ── 总字数 %d" % total)

    print("\n" + "=" * 74)
    print("观察点：极简/老手卡应当明显短于讲解卡；但『命令』本身不应变差。")
    return 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())
