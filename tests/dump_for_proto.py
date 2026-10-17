# -*- coding: utf-8 -*-
"""抓取四张角色卡的真实输出，导出成原型要用的数据。

跑法：python3 tests/dump_for_proto.py > /dev/null 2>&1
输出：tests/_proto_data.json
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nlc.prompts import load_profiles  # noqa: E402
from nlc.providers import get_provider, load_config, test  # noqa: E402
from nlc.translator import translate  # noqa: E402

QUESTION = "找出当前目录下占用空间最大的十个文件夹"
CARDS = ["concise", "expert", "balanced", "teacher"]


def main() -> int:
    cfg = load_config()
    p = get_provider(cfg, "deepseek")
    ok, msg = test(p)
    if not ok:
        print("通道不通：" + msg)
        return 1

    profs = load_profiles()
    out = {"question": QUESTION, "cards": {}}

    for cid in CARDS:
        prof = profs[cid]
        tr = translate(QUESTION, p, profile=prof)
        out["cards"][cid] = {
            "id": cid, "emoji": prof.emoji, "name": prof.name, "desc": prof.desc,
            "temperature": prof.temperature, "max_tokens": prof.max_tokens,
            "command": tr.command,
            "summary": tr.summary,
            "notes": tr.notes,
            "total": len(tr.summary or "") + sum(len(b.meaning) for b in tr.blocks),
            "blocks": [{"i": b.index, "t": b.text, "k": b.kind,
                        "g": b.gap_before, "m": b.meaning} for b in tr.blocks],
        }

    dst = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_proto_data.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("已写入 " + dst)
    for cid in CARDS:
        c = out["cards"][cid]
        print("  %-9s %s 字  %d 个词块" % (cid, c["total"], len(c["blocks"])))
    return 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())
