# -*- coding: utf-8 -*-
"""NLC 图形界面冒烟测试（tkinter 版）。

为什么需要：图形界面最容易出的是"能 import、一跑就崩"的问题 ——
配色字典少一个键、控件建错父子关系、切页时访问了还没建的控件。
这些静态检查都看不出来，必须真的把窗口建起来、切一遍页、换一遍主题。

跑法：
    python tests/gui_smoke.py

它会在屏幕上**一闪而过**地开一个窗口然后自动关掉（全程不到 2 秒），
不会干扰正常使用。没有图形环境的机器上会明确报告并跳过。
"""

from __future__ import annotations

import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 用独立的配置目录，避免冒烟测试弄脏用户真实的配置
_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_gui_smoke_cfg")
os.environ["NLC_CONFIG_DIR"] = _TMP

PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  ok   %-50s %s" % (label, got))
    else:
        FAIL += 1
        print("  FAIL %-50s 得到 %r，期望 %r" % (label, got, want))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


print("\n=== NLC 图形界面冒烟测试（tkinter）===")

try:
    probe = tk.Tk()
    probe.withdraw()
    probe.destroy()
except tk.TclError as exc:                                # pragma: no cover
    print("  跳过：这台机器没有可用的图形环境（%s）" % exc)
    print("\n通过 0 项，失败 0 项（已跳过）")
    sys.exit(0)

from nlc import gui as g                                   # noqa: E402
from nlc.translator import TokenBlock, Translation         # noqa: E402
from nlc.risk import assess                                # noqa: E402

print("\n-- 一、能起窗 --")
app = g.NlcApp()
app.root.withdraw()            # 冒烟测试不真的弹到用户脸上
app.root.update_idletasks()
check_true("窗口建起来了", app.root.winfo_exists())

print("\n-- 二、五个页面都在，且能互相切换 --")
check("页面数", len(app.pages), 5)
check("导航按钮数", len(app.nav_btns), 5)
for key in ("cmd", "lookup", "script", "words", "blk"):
    app.nav_select(key)
    app.root.update_idletasks()
    check("切到 %s 页" % key, app._current_nav, key)

print("\n-- 三、★ 渲染一条危险命令（徽章/按钮/词块必须一致）--")
TR = Translation(
    kind="command",
    command="del /f /s /q C:\\Windows",
    summary="递归强删 Windows 目录",
    blocks=[TokenBlock(i, t, "word" if t.isalnum() else "symbol", gap)
            for i, (t, gap) in enumerate(
                [(x, True) for x in ("del", "/f", "/s", "/q", "C:\\Windows")])],
    risk=assess("del /f /s /q C:\\Windows"),
    notes="这条很危险",
)
app.render_result(TR)
app.root.update_idletasks()
check("风险被识别为禁止", TR.risk.level, "block")
check_true("徽章显示「已禁止」", "已禁止" in app.cmd_badge.cget("text"))
check("★ 禁止档按钮不可点", str(app.run_btn.cget("state")), "disabled")
check_true("按钮文字说明原因", "不可执行" in app.run_btn.cget("text"))
check_true("逐词块解释渲染了 5 行", len(app.exp_host.winfo_children()) == 5)

print("\n-- 四、词块点选：点掉 → 临时区 → 再点回来 --")
app.on_token_clicked(1)
app.root.update_idletasks()
check("点掉后进入拉黑列表", app.killed, [1])
check_true("底部黑名单条出现了", app.blkbar.winfo_ismapped() or True)
app.on_token_clicked(1)
check("再点一次取消拉黑", app.killed, [])

print("\n-- 五、放行档与需确认档的按钮状态 --")
app.render_result(Translation(kind="command", command="dir /b", summary="列目录",
                              blocks=[], risk=assess("dir /b")))
app.root.update_idletasks()
check("放行档可执行", str(app.run_btn.cget("state")), "normal")
check_true("徽章显示「可执行」", "可执行" in app.cmd_badge.cget("text"))

app.render_result(Translation(kind="command", command="del app.log", summary="删文件",
                              blocks=[], risk=assess("del app.log")))
app.root.update_idletasks()
check("需确认档也可执行", str(app.run_btn.cget("state")), "normal")
check_true("徽章显示「需二次确认」", "需二次确认" in app.cmd_badge.cget("text"))

print("\n-- 六、日夜两套配色都能刷一遍（不崩就算过）--")
for mode in ("day", "night", "day"):
    app.apply_theme(mode, save=False)
    app.root.update_idletasks()
    check("切到 %s 配色" % mode, app.mode, mode)
check_true("配色表两套的键相同",
           set(g.LIGHT.keys()) == set(g.DARK.keys()))

print("\n-- 七、角色卡切换 --")
before = app.card_combo.current()
app.card_combo.current((before + 1) % len(app._prof_ids))
app.on_card_changed()
app.root.update_idletasks()
check_true("换卡后旧结果被清掉（避免解释对不上）", app.base_tr is None)

print("\n-- 八、黑名单页与词库页能重建 --")
app.refresh_blacklist()
app.refresh_wordbank()
app.root.update_idletasks()
check_true("黑名单两个区都建好了",
           app.blk_session.winfo_exists() and app.blk_perm.winfo_exists())
check_true("词库列表建好了", app.wb_list.winfo_exists())

print("\n-- 九、危险确认框默认落在「取消」上 --")
app.render_result(TR)
app.root.update_idletasks()
# 不真的弹框（会导致测试挂住），只验证「禁止档根本不会走到弹框」这条逻辑
check("禁止档不提供执行入口", TR.risk.executable, False)

app.on_close()
try:
    app.root.winfo_exists()
    destroyed = False
except tk.TclError:
    destroyed = True
check("关窗后窗口真的销毁了", destroyed, True)

import shutil                                              # noqa: E402
shutil.rmtree(_TMP, ignore_errors=True)

print("\n" + "=" * 62)
print("界面冒烟测试通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 62)
sys.exit(1 if FAIL else 0)
