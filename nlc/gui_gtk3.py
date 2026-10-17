#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NLC 自然语言命令行 —— GTK3 桌面界面。

设计约束（来自这台机器的硬性结论，改代码前先看《Linux开发文档/06_桌面环境/系统设置_统一面板.md》）：
    1. **只用 GTK3**。GTK4 在 llvmpipe 软件渲染下起窗要 3.5 秒以上，zenity 因此被弃用。
    2. **CSS 不能用 `font:` 简写，也不能用 8 位十六进制颜色（#RRGGBBAA）** ——
       GTK3 会**静默丢弃整张样式表**，症状是"界面突然全变默认主题"，极难排查。
    3. **CSS 字符串不能用 `b\"\"\"...\"\"\"`** —— 里面有中文会直接 SyntaxError。
    4. `Gtk.Stack.set_visible_child_name()` **必须在 `show_all()` 之后**，否则被覆盖。
    5. **不用 `Gtk.FlowBox.set_filter_func()`** —— 本机 `invalidate_filter()` 后网格整片空白。
    6. **不用 `set_size_request()` 钉高度** —— 会留下看不见但吃点击的透明区。
    7. 网络调用**必须放到工作线程**，否则界面在等 API 的那两秒里完全卡死。

设计哲学（见《NLC规则详解_v3.1.md》）：
    **AI 只翻译，不执行。** 这个界面里没有任何一处会自动执行命令 ——
    所有执行都必须由用户按下按钮。
"""

from __future__ import annotations

import os
import sys
import threading

# ── 让它既能 `python3 -m nlc.gui`，也能 `python3 /path/gui.py` 直接跑 ──
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from nlc.blacklist import Blacklist  # noqa: E402
from nlc.prompts import build_system_prompt, get_profile, load_profiles  # noqa: E402
from nlc.providers import (OFFLINE, fallback_chain, get_api_key, get_provider,  # noqa: E402
                           load_config, save_config, test as chan_test)
from nlc.risk import ALLOW, BLOCK, CONFIRM, assess  # noqa: E402
from nlc.translator import TranslatorError, compose, lookup, regenerate, translate  # noqa: E402
from nlc.wordbank import WordBank, WordBankError  # noqa: E402

APP_ID = "cn.nlc.gui"
TITLE = "NLC · 自然语言命令行"

LEVEL_TEXT = {ALLOW: "可执行", CONFIRM: "需二次确认", BLOCK: "已禁止"}
LEVEL_ICON = {ALLOW: "✔", CONFIRM: "▲", BLOCK: "✕"}


# --------------------------------------------------------------------------- #
# 配色（与原型同一套，按无障碍规范取色）
# --------------------------------------------------------------------------- #
LIGHT = dict(
    page="#F7F7F8", surface="#FFFFFF", elevated="#F3F3F4", hover="#EBEBED",
    text1="#16181C", text2="#5A5D63", text3="#8C8F96",
    border="#E3E4E7", accent="#1E63A8", ok="#1D9E75", danger="#D93F3E", warn="#B0740F",
    gtkt="Arc-Lighter", icon="Papirus-Light",
)
DARK = dict(
    page="#111318", surface="#181B21", elevated="#20242B", hover="#282D36",
    text1="#E7E9ED", text2="#9DA2AC", text3="#6E7481",
    border="#2A2F38", accent="#7CB2EA", ok="#4DBF97", danger="#EE7371", warn="#DCA34E",
    gtkt="Arc-Dark", icon="Papirus-Dark",
)


#: 设计基准宽度。小于它不缩放，大于它按比例放大。
BASE_WIDTH = 1350.0
MIN_SCALE, MAX_SCALE = 1.0, 1.8


def _screen_width() -> int:
    """当前屏幕逻辑宽度。优先用 Display/Monitor 接口（Gdk.Screen.get_width 已弃用）。"""
    try:
        disp = Gdk.Display.get_default()
        mon = disp.get_primary_monitor() or disp.get_monitor(0)
        return mon.get_geometry().width
    except Exception:
        try:
            return Gdk.Screen.get_default().get_width()
        except Exception:
            return 1280


def scale_for(width: int) -> float:
    """按屏幕宽度算缩放系数。

    为什么需要：这块平板的 X 显示是 2160×1440 / 96 DPI，
    **1 个逻辑像素就等于 1 个物理像素**，而它物理上只有 10 英寸出头。
    于是按"笔记本电脑尺寸"写的界面在平板上只有应有大小的 1/1.6，字小、钮小、不好点。
    ⇒ 按屏幕宽度整体放大，触摸目标才够大。
    """
    return max(MIN_SCALE, min(MAX_SCALE, width / BASE_WIDTH))


_PX = re.compile(r"(-?\d+(?:\.\d+)?)px")


def scale_css(css: str, k: float) -> str:
    """把样式表里所有 `Npx` 按系数放大。这样样式表保持可读，缩放在一处完成。"""
    if abs(k - 1.0) < 0.01:
        return css
    return _PX.sub(lambda m: "%dpx" % round(float(m.group(1)) * k), css)


def build_css(c: dict) -> str:
    """按配色生成样式表。

    ⚠️ 只用 font-size / font-weight，**绝不用 `font:` 简写**；颜色一律 #RRGGBB（不用 8 位）。
    """
    return """
    window, .nlc-root, .page, scrolledwindow, viewport, box {{
        background-color: {page}; background-image: none; color: {text1}; }}

    .hdr {{ background-color: {surface}; background-image: none;
            border-bottom: 1px solid {border}; }}
    .hdr-title {{ font-size: 16px; font-weight: 600; color: {text1}; }}
    .hdr-sub {{ font-size: 11px; color: {text3}; }}
    .card {{ background-color: {surface}; background-image: none;
             border: 1px solid {border}; border-radius: 12px; }}
    .nav {{ background-color: {surface}; background-image: none;
            border-right: 1px solid {border}; }}
    .navitem {{ background-image: none; background-color: transparent; border: none;
                border-radius: 10px; color: {text2}; font-size: 14px; padding: 14px 12px; }}
    .navitem:hover {{ background-color: {hover}; }}
    .navitem.on {{ background-color: {elevated}; color: {accent}; font-weight: 600; }}

    entry, textview, textview text {{ background-image: none; background-color: {elevated};
        color: {text1}; border: 1px solid {border}; border-radius: 10px; font-size: 15px; }}
    entry:focus {{ border-color: {accent}; }}

    button {{ font-size: 14px; border-radius: 10px; padding: 10px 16px;
              background-image: none; background-color: {elevated};
              color: {text1}; border: 1px solid {border}; }}
    button:hover {{ background-color: {hover}; }}
    .primary {{ background-color: {accent}; color: #FFFFFF; border: none; font-weight: 600; }}
    .btn-ok {{ background-color: {ok}; color: #FFFFFF; border: none; font-weight: 600; }}
    .btn-danger {{ background-color: {danger}; color: #FFFFFF; border: none; font-weight: 600; }}
    .btn-off {{ background-color: {hover}; color: {text3}; border: 1px solid {border}; }}

    .badge {{ font-size: 12px; font-weight: 600; border-radius: 999px; padding: 5px 13px;
              background-image: none; }}
    .badge-ok {{ background-color: {ok}; color: #FFFFFF; }}
    .badge-danger {{ background-color: {danger}; color: #FFFFFF; }}
    .badge-off {{ background-color: {hover}; color: {text3}; }}

    .tok {{ font-family: monospace; font-size: 17px; padding: 9px 13px;
            background-image: none; background-color: {elevated}; color: {text1};
            border: 1px solid {border}; border-radius: 8px; }}
    .tok:hover {{ border-color: {accent}; }}
    .tok.kill {{ background-color: {danger}; color: #FFFFFF; border-color: {danger}; }}
    .tok.glue {{ margin-left: 1px; border-radius: 3px; }}
    .tok-gap {{ margin-left: 7px; }}

    .exp {{ font-size: 13px; color: {text2}; }}
    .exp-key {{ font-family: monospace; color: {accent}; font-size: 13px; }}
    .dim {{ color: {text3}; font-size: 12px; }}
    .sect {{ font-size: 12px; color: {text3}; }}
    .sum {{ font-size: 14px; color: {text1}; }}

    row, list {{ background-color: {surface}; color: {text1}; }}
    .rowitem {{ background-color: {surface}; border-bottom: 1px solid {border}; }}
    .rowitem:hover {{ background-color: {hover}; }}
    """.format(**c)


# --------------------------------------------------------------------------- #
# 应用
# --------------------------------------------------------------------------- #
class NlcApp:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.bl = Blacklist()
        self.wb = WordBank()
        self.mode = (self.cfg.get("ui") or {}).get("theme", "day")
        if self.mode not in ("day", "night"):
            self.mode = "day"

        self.base_tr = None          # 上一次翻译结果
        self.killed: list = []       # 临时区：本次被点掉的下标
        self.provider = None
        self.busy = False

        self.win = Gtk.Window(title=TITLE)
        # set_wmclass 已废弃，用 set_program_class 代替
        Gdk.set_program_class("Nlc")
        # ★ 不用 move() 定位：xfwm4 的 placement_mode 会覆盖它。用居中让 WM 自己算。
        self.win.set_position(Gtk.WindowPosition.CENTER)
        # ★ 不设默认尺寸：这块屏幕逻辑宽度就是 2160，写死 1180 反而会超屏被裁。
        #   直接最大化，让 WM 给尺寸。
        self.win.connect("destroy", Gtk.main_quit)

        # 缩放必须在窗口建好之后算（此时 GTK 已初始化，能拿到屏幕尺寸）
        self.scale = scale_for(_screen_width())
        self._font_pt = max(10, round(10 * self.scale))

        self.css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._built = False
        self._build()
        self.apply_theme(self.mode, save=False)
        self.refresh_provider_label()
        self.win.maximize()          # 平板上直接全屏
        self.win.show_all()          # ★ 必须先 show_all
        self.blkbar.set_visible(False)
        self.stack.set_visible_child_name("cmd")   # ★ 再切页
        self.nav_select("cmd")
        self.refresh_blacklist()
        self.refresh_wordbank()
        self._built = True

        # 演示/截图用：设 NLC_GUI_DEMO=1 时，启动后自动跑一次翻译，
        # 省得每次都要手动点一下才能看到词块与解释。（不影响正常使用）
        if os.environ.get("NLC_GUI_DEMO"):
            GLib.timeout_add(900, self._demo_kick)

    # ------------------------------------------------------------------ #
    # 组装
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.get_style_context().add_class("nlc-root")
        self.win.add(root)

        # —— 顶栏 ——
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        hdr.get_style_context().add_class("hdr")
        hdr.set_border_width(12)

        logo = Gtk.Label(label="▍")
        logo.get_style_context().add_class("hdr-title")
        hdr.pack_start(logo, False, False, 0)

        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        t1 = Gtk.Label(label="NLC", xalign=0)
        t1.get_style_context().add_class("hdr-title")
        t2 = Gtk.Label(label="自然语言命令行 · AI 只翻译，不执行", xalign=0)
        t2.get_style_context().add_class("hdr-sub")
        tb.pack_start(t1, False, False, 0)
        tb.pack_start(t2, False, False, 0)
        hdr.pack_start(tb, False, False, 0)

        hdr.pack_start(Gtk.Label(label=""), True, True, 0)

        hdr.pack_start(Gtk.Label(label="角色卡"), False, False, 0)
        self.card_combo = Gtk.ComboBoxText()
        self.profiles = load_profiles()
        cur = (self.cfg.get("ui") or {}).get("prompt_profile", "concise")
        for i, pid in enumerate(self.profiles):
            p = self.profiles[pid]
            self.card_combo.append(pid, "%s %s" % (p.emoji, p.name))
            if pid == cur:
                self.card_combo.set_active(i)
        if self.card_combo.get_active() < 0:
            self.card_combo.set_active(0)
        self.card_combo.connect("changed", self.on_card_changed)
        # ⚠️ 不能在这里 connect：下面 set_active() 会立刻触发 changed，
        #    而 render_result() 要用的控件（tok_flow / cmd_badge…）此时还没建好。
        #    所以只记下当前值，等 _build() 全部完成后由 _built 标志放行。
        hdr.pack_start(self.card_combo, False, False, 0)

        self.prov_lbl = Gtk.Label(label="")
        self.prov_lbl.get_style_context().add_class("hdr-sub")
        hdr.pack_start(self.prov_lbl, False, False, 0)

        self.theme_btn = Gtk.Button(label="🌙 夜晚")
        self.theme_btn.set_size_request(-1, self.s(44))   # 只钉高度下限给触摸用
        self.theme_btn.connect("clicked", self.on_theme)
        hdr.pack_start(self.theme_btn, False, False, 0)

        gear = Gtk.Button(label="⚙")
        gear.set_size_request(-1, self.s(44))
        gear.connect("clicked", self.on_settings)
        hdr.pack_start(gear, False, False, 0)

        root.pack_start(hdr, False, False, 0)

        # —— 主体：左导航 + 右内容 ——
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        root.pack_start(body, True, True, 0)

        nav = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        nav.get_style_context().add_class("nav")
        nav.set_border_width(10)
        nav.set_size_request(self.s(176), -1)
        self.nav_btns = {}
        for key, label in (("cmd", "命令行"), ("lookup", "查词"), ("script", "对话式脚本"),
                           ("words", "词库"), ("blk", "黑名单")):
            b = Gtk.Button(label=label)
            b.get_style_context().add_class("navitem")
            b.set_size_request(-1, self.s(52))
            b.connect("clicked", lambda _w, k=key: self.nav_select(k))
            nav.pack_start(b, False, False, 0)
            self.nav_btns[key] = b
        body.pack_start(nav, False, False, 0)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        body.pack_start(self.stack, True, True, 0)

        self.stack.add_named(self._page_cmd(), "cmd")
        self.stack.add_named(self._page_lookup(), "lookup")
        self.stack.add_named(self._page_script(), "script")
        self.stack.add_named(self._page_words(), "words")
        self.stack.add_named(self._page_blk(), "blk")

    # ------------------------------------------------------------------ #
    # 各页
    # ------------------------------------------------------------------ #
    @staticmethod
    def _card(spacing: int = 10) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
        box.get_style_context().add_class("card")
        box.set_border_width(16)
        return box

    def _page_cmd(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_border_width(14)
        page.get_style_context().add_class("page")

        entrycard = self._card(8)
        lab = Gtk.Label(label="用一句话说明你想干什么", xalign=0)
        lab.get_style_context().add_class("sect")
        entrycard.pack_start(lab, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.cmd_entry = Gtk.Entry()
        self.cmd_entry.set_text("找出当前目录下占用空间最大的十个文件夹")
        self.cmd_entry.set_size_request(-1, self.s(48))
        self.cmd_entry.connect("activate", lambda _w: self.do_translate())
        row.pack_start(self.cmd_entry, True, True, 0)
        go = Gtk.Button(label="翻译")
        go.get_style_context().add_class("primary")
        go.set_size_request(self.s(96), self.s(48))
        go.connect("clicked", lambda _w: self.do_translate())
        row.pack_start(go, False, False, 0)
        entrycard.pack_start(row, False, False, 0)

        chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for s in ("看看谁占着 8080 端口", "统计日志里错误出现了几次", "磁盘占用"):
            c = Gtk.Button(label=s)
            c.connect("clicked", lambda _w, t=s: self.cmd_entry.set_text(t))
            chips.pack_start(c, False, False, 0)
        entrycard.pack_start(chips, False, False, 0)
        page.pack_start(entrycard, False, False, 0)

        # 结果卡
        res = self._card(10)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.cmd_badge = Gtk.Label(label="—")
        self.cmd_badge.get_style_context().add_class("badge")
        self.cmd_badge.get_style_context().add_class("badge-off")
        head.pack_start(self.cmd_badge, False, False, 0)
        self.cmd_title = Gtk.Label(label="点掉不对的词块，再点「重新生成」", xalign=0)
        self.cmd_title.get_style_context().add_class("dim")
        head.pack_start(self.cmd_title, True, True, 0)
        res.pack_start(head, False, False, 0)

        self.tok_flow = Gtk.FlowBox()
        self.tok_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.tok_flow.set_valign(Gtk.Align.START)
        self.tok_flow.set_max_children_per_line(30)
        self.tok_flow.set_row_spacing(8)
        tokwrap = Gtk.Frame()
        tokwrap.set_shadow_type(Gtk.ShadowType.NONE)
        tokwrap.add(self.tok_flow)
        res.pack_start(tokwrap, False, False, 0)

        self.sum_lbl = Gtk.Label(label="", xalign=0)
        self.sum_lbl.get_style_context().add_class("sum")
        self.sum_lbl.set_line_wrap(True)
        res.pack_start(self.sum_lbl, False, False, 0)

        self.exp_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        res.pack_start(self.exp_box, False, False, 0)

        self.notes_lbl = Gtk.Label(label="", xalign=0)
        self.notes_lbl.get_style_context().add_class("dim")
        self.notes_lbl.set_line_wrap(True)
        res.pack_start(self.notes_lbl, False, False, 0)

        acts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.run_btn = Gtk.Button(label="执行")
        self.run_btn.set_size_request(-1, self.s(46))
        self.run_btn.set_sensitive(False)
        self.run_btn.connect("clicked", lambda _w: self.on_execute())
        acts.pack_start(self.run_btn, False, False, 0)
        regen = Gtk.Button(label="重新生成")
        regen.set_size_request(-1, self.s(46))
        regen.connect("clicked", lambda _w: self.do_regenerate())
        acts.pack_start(regen, False, False, 0)
        copyb = Gtk.Button(label="复制命令")
        copyb.set_size_request(-1, self.s(46))
        copyb.connect("clicked", self.on_copy)
        acts.pack_start(copyb, False, False, 0)
        res.pack_start(acts, False, False, 0)

        self.blkbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.blkbar_lbl = Gtk.Label(label="", xalign=0)
        self.blkbar_lbl.get_style_context().add_class("dim")
        self.blkbar.pack_start(self.blkbar_lbl, True, True, 0)
        promote = Gtk.Button(label="升级到永久区")
        promote.connect("clicked", lambda _w: self.on_promote())
        self.blkbar.pack_start(promote, False, False, 0)
        res.pack_start(self.blkbar, False, False, 0)

        res.set_valign(Gtk.Align.START)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(res)
        page.pack_start(scroll, True, True, 0)
        return page

    def _page_lookup(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_border_width(14)
        page.get_style_context().add_class("page")
        c = self._card(8)
        lab = Gtk.Label(label="输入一个词，看它对应哪个命令词块", xalign=0)
        lab.get_style_context().add_class("sect")
        c.pack_start(lab, False, False, 0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.lk_entry = Gtk.Entry()
        self.lk_entry.set_text("查端口占用")
        self.lk_entry.set_size_request(-1, self.s(48))
        self.lk_entry.connect("activate", lambda _w: self.do_lookup())
        row.pack_start(self.lk_entry, True, True, 0)
        b = Gtk.Button(label="查词")
        b.get_style_context().add_class("primary")
        b.set_size_request(self.s(96), self.s(48))
        b.connect("clicked", lambda _w: self.do_lookup())
        row.pack_start(b, False, False, 0)
        c.pack_start(row, False, False, 0)
        page.pack_start(c, False, False, 0)

        rc = self._card(10)
        self.lk_token = Gtk.Label(label="—")
        self.lk_token.get_style_context().add_class("tok")
        self.lk_token.set_xalign(0)
        rc.pack_start(self.lk_token, False, False, 0)
        self.lk_sum = Gtk.Label(label="", xalign=0)
        self.lk_sum.get_style_context().add_class("sum")
        self.lk_sum.set_line_wrap(True)
        rc.pack_start(self.lk_sum, False, False, 0)
        self.lk_notes = Gtk.Label(label="", xalign=0)
        self.lk_notes.get_style_context().add_class("exp")
        self.lk_notes.set_line_wrap(True)
        rc.pack_start(self.lk_notes, False, False, 0)
        page.pack_start(rc, False, False, 0)
        page.pack_start(Gtk.Label(label=""), True, True, 0)
        return page

    def _page_script(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_border_width(14)
        page.get_style_context().add_class("page")
        c = self._card(8)
        lab = Gtk.Label(label="把刚才用过的命令拼成一个脚本一起跑（不要求逻辑连贯）", xalign=0)
        lab.get_style_context().add_class("sect")
        c.pack_start(lab, False, False, 0)
        self.sc_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        c.pack_start(self.sc_list, False, False, 0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        add = Gtk.Button(label="把当前命令行加进来")
        add.connect("clicked", lambda _w: self.on_add_to_script())
        row.pack_start(add, False, False, 0)
        run = Gtk.Button(label="组合并执行")
        run.get_style_context().add_class("btn-ok")
        run.connect("clicked", lambda _w: self.do_compose())
        row.pack_start(run, False, False, 0)
        c.pack_start(row, False, False, 0)
        self.sc_out = Gtk.Label(label="", xalign=0)
        self.sc_out.get_style_context().add_class("tok")
        self.sc_out.set_line_wrap(True)
        self.sc_out.set_selectable(True)
        c.pack_start(self.sc_out, False, False, 0)
        page.pack_start(c, False, False, 0)
        page.pack_start(Gtk.Label(label=""), True, True, 0)
        return page

    def _page_words(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_border_width(14)
        page.get_style_context().add_class("page")

        sw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        sw.get_style_context().add_class("card")
        sw.set_border_width(14)
        on = (self.cfg.get("ui") or {}).get("translate_enabled", True)
        self.tr_switch = Gtk.Switch()
        self.tr_switch.set_active(bool(on))
        self.tr_switch.connect("notify::active", self.on_trans_switch)
        sw.pack_start(self.tr_switch, False, False, 0)
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.tr_title = Gtk.Label(xalign=0)
        self.tr_title.set_markup("<b>翻译功能</b>")
        self.tr_sub = Gtk.Label(xalign=0)
        self.tr_sub.get_style_context().add_class("dim")
        v.pack_start(self.tr_title, False, False, 0)
        v.pack_start(self.tr_sub, False, False, 0)
        sw.pack_start(v, True, True, 0)
        page.pack_start(sw, False, False, 0)

        c = self._card(8)
        h = Gtk.Label(label="我的词库（一个自然语言词只对应一个命令词块；重名会报错不覆盖）", xalign=0)
        h.get_style_context().add_class("sect")
        c.pack_start(h, False, False, 0)
        self.wb_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        c.pack_start(self.wb_list, False, False, 0)
        addrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.wb_k = Gtk.Entry()
        self.wb_k.set_placeholder_text("自然语言词，如「看磁盘」")
        self.wb_v = Gtk.Entry()
        self.wb_v.set_placeholder_text("命令词块，如「df」")
        addrow.pack_start(self.wb_k, True, True, 0)
        addrow.pack_start(self.wb_v, True, True, 0)
        ab = Gtk.Button(label="添加")
        ab.connect("clicked", lambda _w: self.on_wb_add())
        addrow.pack_start(ab, False, False, 0)
        sh = Gtk.Button(label="生成分享码")
        sh.connect("clicked", lambda _w: self.on_share_code())
        addrow.pack_start(sh, False, False, 0)
        c.pack_start(addrow, False, False, 0)
        self.wb_msg = Gtk.Label(label="", xalign=0)
        self.wb_msg.get_style_context().add_class("dim")
        self.wb_msg.set_line_wrap(True)
        self.wb_msg.set_selectable(True)
        c.pack_start(self.wb_msg, False, False, 0)
        page.pack_start(c, False, False, 0)
        self.update_trans_text()
        return page

    def _page_blk(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        page.set_border_width(14)
        page.get_style_context().add_class("page")

        a = self._card(8)
        t = Gtk.Label(label="临时区（按下标记 · 新查询自动清空）", xalign=0)
        t.get_style_context().add_class("sect")
        a.pack_start(t, False, False, 0)
        self.blk_session = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        a.pack_start(self.blk_session, False, False, 0)
        p = Gtk.Button(label="全部升级到永久区")
        p.connect("clicked", lambda _w: self.on_promote())
        a.pack_start(p, False, False, 0)
        n = Gtk.Label(label="临时区按下标记：一次查询里 | 可能出现两次，只有下标能分清点的是哪一个。",
                      xalign=0)
        n.get_style_context().add_class("dim")
        n.set_line_wrap(True)
        a.pack_start(n, False, False, 0)
        page.pack_start(a, True, True, 0)

        b = self._card(8)
        t2 = Gtk.Label(label="永久区（按词块文本 · 跨会话生效）", xalign=0)
        t2.get_style_context().add_class("sect")
        b.pack_start(t2, False, False, 0)
        self.blk_perm = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        b.pack_start(self.blk_perm, False, False, 0)
        n2 = Gtk.Label(label="永久区按文本记：跨命令时「第 8 个词块」毫无意义，只有文本能跨命令生效。",
                       xalign=0)
        n2.get_style_context().add_class("dim")
        n2.set_line_wrap(True)
        b.pack_start(n2, False, False, 0)
        page.pack_start(b, True, True, 0)
        return page

    # ------------------------------------------------------------------ #
    # 主题
    # ------------------------------------------------------------------ #
    def s(self, n: int) -> int:
        """把设计尺寸换算成当前屏幕的尺寸。"""
        return max(1, int(round(n * self.scale)))

    def _demo_kick(self) -> bool:
        """演示模式下自动触发一次翻译。返回 False 让 GLib 不再重复调用。"""
        try:
            self.do_translate()
        except Exception:
            pass
        return False

    def apply_theme(self, mode: str, save: bool = True) -> None:
        c = LIGHT if mode == "day" else DARK
        self.mode = mode
        # ⚠️ 中文不能放进 b\"\"\"…\"\"\"，所以这里用普通字符串 + encode
        # ★ 普通字符串 + encode：中文不能放进 b"""…"""，否则 SyntaxError
        self.css.load_from_data(scale_css(build_css(c), self.scale).encode("utf-8"))
        # 让整个 App 的控件跟随主题（立即生效，不需要重启）
        st = Gtk.Settings.get_default()
        if st is not None:
            st.set_property("gtk-theme-name", c["gtkt"])
            st.set_property("gtk-icon-theme-name", c["icon"])
            # 没被我样式表覆盖的控件（下拉框、开关、滚动条…）靠这条跟随缩放
            st.set_property("gtk-font-name", "Noto Sans CJK SC %d" % self._font_pt)
        self.theme_btn.set_label("☀ 白天" if mode == "night" else "🌙 夜晚")
        self.cfg.setdefault("ui", {})["theme"] = mode
        if save:
            save_config(self.cfg)

    def on_theme(self, _b) -> None:
        self.apply_theme("night" if self.mode == "day" else "day")

    # ------------------------------------------------------------------ #
    # 导航
    # ------------------------------------------------------------------ #
    def nav_select(self, key: str) -> None:
        for k, b in self.nav_btns.items():
            ctx = b.get_style_context()
            if k == key:
                ctx.add_class("on")
            else:
                ctx.remove_class("on")
        # ★ 必须已经 show_all 过，否则这次切换会被覆盖
        self.stack.set_visible_child_name(key)
        if key == "blk":
            self.refresh_blacklist()

    # ------------------------------------------------------------------ #
    # 通道
    # ------------------------------------------------------------------ #
    def pick_provider(self):
        for pid in fallback_chain(self.cfg):
            if pid == OFFLINE:
                continue
            p = get_provider(self.cfg, pid)
            if not p:
                continue
            if p.needs_key and not get_api_key(p.id, p.key_env):
                continue
            self.provider = p
            return p
        self.provider = None
        return None

    def refresh_provider_label(self) -> None:
        p = self.pick_provider()
        self.prov_lbl.set_text("● " + (p.name if p else "没有可用通道"))

    def active_profile(self):
        pid = self.card_combo.get_active_id() or "concise"
        return self.profiles.get(pid) or get_profile("concise")

    def on_card_changed(self, _c) -> None:
        if not getattr(self, "_built", False):
            return
        pid = self.card_combo.get_active_id()
        self.cfg.setdefault("ui", {})["prompt_profile"] = pid
        save_config(self.cfg)
        # 换卡后旧解释不再对应当前卡，清掉避免误导
        self.base_tr = None
        self.killed = []
        self.render_result(None)

    # ------------------------------------------------------------------ #
    # 异步：所有网络调用都丢到工作线程
    # ------------------------------------------------------------------ #
    def run_async(self, fn, on_done, on_fail) -> None:
        if self.busy:
            return
        self.busy = True
        self.cmd_title.set_text("正在请求模型…")

        def worker():
            try:
                r = fn()
            except Exception as exc:                      # noqa: BLE001
                GLib.idle_add(_finish_err, exc)
            else:
                GLib.idle_add(_finish_ok, r)

        def _finish_ok(r):
            self.busy = False
            on_done(r)
            return False      # ★ 必须返回 False，返回 True 会变成死循环

        def _finish_err(exc):
            self.busy = False
            on_fail(exc)
            return False

        threading.Thread(target=worker, daemon=True).start()

    def _need_provider(self) -> bool:
        p = self.pick_provider()
        if not p:
            self.cmd_title.set_text("没有可用的 AI 通道 —— 请先在设置里填 API Key")
            return False
        return True

    # ------------------------------------------------------------------ #
    # 命令行页
    # ------------------------------------------------------------------ #
    def do_translate(self) -> None:
        if not self._need_provider():
            return
        text = self.cmd_entry.get_text().strip()
        if not text:
            return
        self.killed = []
        self.bl.start_query(text)
        prof = self.active_profile()
        chain = self.bl.exclude_texts()

        def job():
            return translate(text, self.provider, exclude_texts=chain, profile=prof)

        self.run_async(job, self.render_result,
                       lambda e: self.cmd_title.set_text("翻译失败：" + str(e)))

    def do_regenerate(self) -> None:
        if not self._need_provider() or self.base_tr is None:
            return
        text = self.cmd_entry.get_text().strip()
        prof = self.active_profile()
        killed = list(self.killed)
        base = self.base_tr
        chain = self.bl.exclude_texts()

        def job():
            return regenerate(text, base, killed, self.provider,
                              exclude_texts=chain, profile=prof)

        self.killed = []
        self.run_async(job, self.render_result,
                       lambda e: self.cmd_title.set_text("重新生成失败：" + str(e)))

    def do_lookup(self) -> None:
        if not self._need_provider():
            return
        term = self.lk_entry.get_text().strip()
        if not term:
            return
        prof = self.active_profile()
        self.lk_sum.set_text("正在查…")

        def job():
            return lookup(term, self.provider, profile=prof)

        def done(tr):
            self.lk_token.set_text(tr.command)
            self.lk_sum.set_text(tr.summary or "")
            self.lk_notes.set_text((tr.blocks[0].meaning if tr.blocks else "") + "\n" + (tr.notes or ""))

        self.run_async(job, done, lambda e: self.lk_sum.set_text("查词失败：" + str(e)))

    def render_result(self, tr) -> None:
        # 清空词块区
        for ch in self.tok_flow.get_children():
            self.tok_flow.remove(ch)
        for ch in self.exp_box.get_children():
            self.exp_box.remove(ch)

        if tr is None:
            self.cmd_badge.set_text("—")
            for c in ("badge-ok", "badge-danger"):
                self.cmd_badge.get_style_context().remove_class(c)
            self.cmd_badge.get_style_context().add_class("badge-off")
            self.sum_lbl.set_text("")
            self.notes_lbl.set_text("")
            self.run_btn.set_sensitive(False)
            self.blkbar.set_visible(False)
            return

        self.base_tr = tr
        self.sum_lbl.set_text("%s —— %s" % (tr.command, tr.summary or ""))
        self.notes_lbl.set_text(tr.notes or "")

        # 风险徽章（颜色 + 图标文字，不只靠颜色）
        a = tr.risk
        ctx = self.cmd_badge.get_style_context()
        for c in ("badge-ok", "badge-danger", "badge-off"):
            ctx.remove_class(c)
        if a:
            self.cmd_badge.set_text("%s %s" % (LEVEL_ICON.get(a.level, "?"),
                                               LEVEL_TEXT.get(a.level, a.level)))
            ctx.add_class("badge-ok" if a.level == ALLOW else
                          ("badge-danger" if a.level == CONFIRM else "badge-off"))
            self.cmd_title.set_text(a.reason)
            ok = a.executable
        else:
            self.cmd_badge.set_text("—")
            ctx.add_class("badge-off")
            ok = False

        btnctx = self.run_btn.get_style_context()
        for c in ("btn-ok", "btn-danger", "btn-off"):
            btnctx.remove_class(c)
        btnctx.add_class("btn-ok" if (a and a.level == ALLOW) else
                         ("btn-danger" if (a and a.level == CONFIRM) else "btn-off"))
        self.run_btn.set_label("执行" if (a and a.level == ALLOW) else
                               ("执行（需确认）" if (a and a.level == CONFIRM) else "已禁止，不可执行"))
        self.run_btn.set_sensitive(bool(ok))

        # 词块
        for b in tr.blocks:
            txt = b.text
            btn = Gtk.Button(label=txt)
            ctx2 = btn.get_style_context()
            ctx2.add_class("tok")
            if not b.gap_before:
                ctx2.add_class("glue")     # ★ 视觉粘连：1px 缝，但仍可独立点击
            else:
                ctx2.add_class("tok-gap")
            if b.index in self.killed:
                ctx2.add_class("kill")
            btn.set_tooltip_text(b.meaning or "")
            btn.connect("clicked", self.on_token_clicked, b.index)
            self.tok_flow.add(btn)
        self.tok_flow.show_all()

        # 逐词块解释
        for b in tr.blocks:
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            k = Gtk.Label(label=b.text)
            k.get_style_context().add_class("exp-key")
            k.set_size_request(self.s(62), -1)
            k.set_xalign(1)
            line.pack_start(k, False, False, 0)
            m = Gtk.Label(label=b.meaning or "", xalign=0)
            m.get_style_context().add_class("exp")
            m.set_line_wrap(True)
            line.pack_start(m, True, True, 0)
            self.exp_box.pack_start(line, False, False, 0)
        self.exp_box.show_all()

        self.render_blkbar()

    def on_token_clicked(self, _btn, index: int) -> None:
        if self.base_tr is None:
            return
        if index in self.killed:
            self.killed.remove(index)
            self.bl.unkill(index)
        else:
            self.killed.append(index)
            blk = next((b for b in self.base_tr.blocks if b.index == index), None)
            self.bl.kill(index, blk.text if blk else "", self.base_tr.command)
        self.render_result(self.base_tr)

    def render_blkbar(self) -> None:
        if not self.killed or self.base_tr is None:
            self.blkbar.set_visible(False)
            return
        names = []
        for i in sorted(self.killed):
            b = next((x for x in self.base_tr.blocks if x.index == i), None)
            if b:
                names.append(b.text)
        self.blkbar_lbl.set_text("临时区已拉黑 %d 个词块：%s —— 重新生成时会保留其余、只替换这些"
                                 % (len(names), " ".join(names)))
        self.blkbar.show_all()

    def on_promote(self) -> None:
        moved = self.bl.promote()
        self.killed = []
        if self.base_tr is not None:
            self.render_result(self.base_tr)
        self.refresh_blacklist()
        self.cmd_title.set_text("已升级到永久区：%s" % (" ".join(moved) if moved else "（没有可升级的）"))

    def on_execute(self) -> None:
        """★ 执行入口。整个 NLC 只有这里、且只有用户按下按钮，才会真的跑命令。"""
        if self.base_tr is None:
            return
        a = self.base_tr.risk
        if a and a.level == BLOCK:
            return
        dlg = Gtk.MessageDialog(
            transient_for=self.win, modal=True, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE)
        dlg.set_markup("<b>确认要执行吗</b>")
        dlg.format_secondary_text(
            "%s\n\nNLC 本身不执行任何东西 —— 只有你按下按钮才会执行。" % self.base_tr.command)
        dlg.add_button("取消", Gtk.ResponseType.CANCEL)
        dlg.add_button("执行", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.CANCEL)   # ★ 默认落在「取消」上
        if dlg.run() == Gtk.ResponseType.OK:
            self.cmd_title.set_text("版本 0.1 尚未接入执行后端（执行权归属设计上单独实现）")
        dlg.destroy()

    def on_copy(self, _b) -> None:
        if self.base_tr is None:
            return
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(self.base_tr.command, -1)
        self.cmd_title.set_text("已复制到剪贴板")

    # ------------------------------------------------------------------ #
    # 脚本页
    # ------------------------------------------------------------------ #
    def on_add_to_script(self) -> None:
        if self.base_tr is None:
            return
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lab = Gtk.Label(label=self.base_tr.command, xalign=0)
        lab.get_style_context().add_class("exp-key")
        row.pack_start(lab, True, True, 0)
        rm = Gtk.Button(label="移除")
        rm.connect("clicked", lambda _w, r=row: self.sc_list.remove(r))
        row.pack_start(rm, False, False, 0)
        self.sc_list.pack_start(row, False, False, 0)
        self.sc_list.show_all()

    def do_compose(self) -> None:
        if not self._need_provider():
            return
        cmds = []
        for ch in self.sc_list.get_children():
            for sub in ch.get_children():
                if isinstance(sub, Gtk.Label):
                    cmds.append(sub.get_text())
        if not cmds:
            self.sc_out.set_text("先「把当前命令行加进来」几条")
            return
        prof = self.active_profile()

        def job():
            return compose(cmds, self.provider, profile=prof)

        def done(tr):
            self.sc_out.set_text(tr.command)
            self.sc_out.grab_focus()

        self.run_async(job, done, lambda e: self.sc_out.set_text("组合失败：" + str(e)))

    # ------------------------------------------------------------------ #
    # 词库页
    # ------------------------------------------------------------------ #
    def update_trans_text(self) -> None:
        on = (self.cfg.get("ui") or {}).get("translate_enabled", True)
        self.tr_title.set_markup("<b>翻译功能：%s</b>" % ("开着" if on else "关着"))
        self.tr_sub.set_text("走 AI 翻译；词库命中的结果会额外标注" if on
                             else "只查词库，不调 AI。回车直接跑对应命令（危险命令仍需确认）")

    def on_trans_switch(self, sw, _p) -> None:
        self.cfg.setdefault("ui", {})["translate_enabled"] = bool(sw.get_active())
        save_config(self.cfg)
        self.update_trans_text()

    def refresh_wordbank(self) -> None:
        for ch in self.wb_list.get_children():
            self.wb_list.remove(ch)
        if not len(self.wb):
            lab = Gtk.Label(label="（空）", xalign=0)
            lab.get_style_context().add_class("dim")
            self.wb_list.pack_start(lab, False, False, 0)
        for e in self.wb.all():
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            a = Gtk.Label(label=e.term, xalign=0)
            a.set_size_request(self.s(170), -1)
            row.pack_start(a, False, False, 0)
            b = Gtk.Label(label=e.token, xalign=0)
            b.get_style_context().add_class("exp-key")
            b.set_size_request(self.s(130), -1)
            row.pack_start(b, False, False, 0)
            flag = "自己填的" if e.verified else "⚠ 未经审核"
            f = Gtk.Label(label=flag, xalign=0)
            f.get_style_context().add_class("dim")
            row.pack_start(f, True, True, 0)
            rm = Gtk.Button(label="删除")
            rm.connect("clicked", lambda _w, t=e.term: self.on_wb_remove(t))
            row.pack_start(rm, False, False, 0)
            self.wb_list.pack_start(row, False, False, 0)
        self.wb_list.show_all()

    def on_wb_add(self) -> None:
        k = self.wb_k.get_text().strip()
        v = self.wb_v.get_text().strip()
        if not k or not v:
            return
        try:
            self.wb.add(k, v)
        except WordBankError as exc:
            self.wb_msg.set_text(str(exc))
            return
        self.wb_k.set_text("")
        self.wb_v.set_text("")
        self.wb_msg.set_text("已添加：%s → %s" % (k, v))
        self.refresh_wordbank()

    def on_wb_remove(self, term: str) -> None:
        self.wb.remove(term)
        self.refresh_wordbank()

    def on_share_code(self) -> None:
        code = self.wb.export_code()
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(code, -1)
        self.wb_msg.set_text("分享码已复制到剪贴板：\n" + code)

    # ------------------------------------------------------------------ #
    # 黑名单页
    # ------------------------------------------------------------------ #
    def refresh_blacklist(self) -> None:
        for box, items, is_perm in ((self.blk_session, self.bl.session, False),
                                    (self.blk_perm, list(self.bl.permanent.values()), True)):
            for ch in box.get_children():
                box.remove(ch)
            if not items:
                lab = Gtk.Label(label="（空）", xalign=0)
                lab.get_style_context().add_class("dim")
                box.pack_start(lab, False, False, 0)
                continue
            for it in items:
                if is_perm:
                    txt = "%s    %s %s" % (it.text, it.added, it.reason or "")
                    act = Gtk.Button(label="解除")
                    act.connect("clicked", lambda _w, t=it.text: self.on_unban(t))
                else:
                    txt = "[%d] %s" % (it.index, it.text)
                    act = Gtk.Button(label="取消")
                    act.connect("clicked", lambda _w, i=it.index: self.on_unkill(i))
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                lab = Gtk.Label(label=txt, xalign=0)
                lab.get_style_context().add_class("exp")
                row.pack_start(lab, True, True, 0)
                row.pack_start(act, False, False, 0)
                box.pack_start(row, False, False, 0)
        self.blk_session.show_all()
        self.blk_perm.show_all()

    def on_unban(self, text: str) -> None:
        self.bl.unban(text)
        self.refresh_blacklist()

    def on_unkill(self, index: int) -> None:
        if index in self.killed:
            self.killed.remove(index)
        self.bl.unkill(index)
        if self.base_tr is not None:
            self.render_result(self.base_tr)
        self.refresh_blacklist()

    # ------------------------------------------------------------------ #
    # 设置
    # ------------------------------------------------------------------ #
    def on_settings(self, _b) -> None:
        dlg = Gtk.Dialog(title="设置", transient_for=self.win, modal=True)
        dlg.set_default_size(self.s(560), self.s(620))
        dlg.add_button("关闭", Gtk.ResponseType.CLOSE)
        area = dlg.get_content_area()
        area.set_border_width(16)
        area.set_spacing(8)

        h = Gtk.Label(label="AI 通道（降级链：云端 → 本地 → 局域网 → 离线兜底）", xalign=0)
        h.get_style_context().add_class("sect")
        area.pack_start(h, False, False, 0)
        for pid in fallback_chain(self.cfg):
            if pid == OFFLINE:
                t = "离线知识库兜底 —— 永远可用"
            else:
                p = get_provider(self.cfg, pid)
                if not p:
                    continue
                if p.needs_key and not get_api_key(p.id, p.key_env):
                    t = "%s —— 未填密钥" % p.name
                else:
                    # ★ 这里绝不能调 chan_test()：那是真实网络请求，
                    #   放在 GTK 主线程里会把整个界面冻住好几秒。
                    t = "%s —— 已填密钥（通道体检请用命令行 nlc test）" % p.name
            lab = Gtk.Label(label="  " + t, xalign=0)
            lab.get_style_context().add_class("exp")
            area.pack_start(lab, False, False, 0)

        area.pack_start(Gtk.Separator(), False, False, 4)
        h2 = Gtk.Label(label="完全访问模式（对照 Codex 的 danger-full-access）", xalign=0)
        h2.get_style_context().add_class("sect")
        area.pack_start(h2, False, False, 0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        cur = bool((self.cfg.get("ui") or {}).get("danger_full_access", False))
        sw = Gtk.Switch()
        sw.set_active(cur)
        sw.connect("notify::active", self.on_danger_switch)
        row.pack_start(sw, False, False, 0)
        sub = Gtk.Label(xalign=0)
        sub.get_style_context().add_class("dim")
        sub.set_line_wrap(True)
        sub.set_text("开启后「需确认」档不再弹框 —— 但「禁止」档仍然点不动，这一档没有开关。")
        row.pack_start(sub, True, True, 0)
        area.pack_start(row, False, False, 0)

        area.pack_start(Gtk.Separator(), False, False, 4)
        h3 = Gtk.Label(label="提示词角色卡：当前卡对 translate 的完整系统提示词", xalign=0)
        h3.get_style_context().add_class("sect")
        area.pack_start(h3, False, False, 0)
        sw2 = Gtk.ScrolledWindow()
        sw2.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        tv = Gtk.TextView()
        tv.set_editable(False)
        tv.set_monospace(True)
        tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        tv.get_buffer().set_text(build_system_prompt(self.active_profile(), "translate"))
        sw2.add(tv)
        sw2.set_size_request(-1, self.s(240))
        area.pack_start(sw2, True, True, 0)

        dlg.show_all()
        dlg.run()
        dlg.destroy()

    def on_danger_switch(self, sw, _p) -> None:
        turn_on = bool(sw.get_active())
        if turn_on:
            d = Gtk.MessageDialog(transient_for=self.win, modal=True,
                                  message_type=Gtk.MessageType.WARNING,
                                  buttons=Gtk.ButtonsType.NONE)
            d.set_markup("<b>完全访问模式</b>")
            d.format_secondary_text("这会跳过所有危险确认。\n请手动输入确认短语「我明白风险」：")
            e = Gtk.Entry()
            e.set_size_request(self.s(280), -1)
            d.vbox.pack_start(e, False, False, 6)
            d.show_all()
            d.add_button("取消", Gtk.ResponseType.CANCEL)
            d.add_button("确定", Gtk.ResponseType.OK)
            resp = d.run()
            ok = (resp == Gtk.ResponseType.OK and e.get_text().strip() == "我明白风险")
            d.destroy()
            if not ok:
                sw.set_active(False)
                return
        self.cfg.setdefault("ui", {})["danger_full_access"] = turn_on
        save_config(self.cfg)
        self.cmd_title.set_text("完全访问模式已%s" % ("开启（危险提示不再弹出）" if turn_on else "关闭"))


def main() -> int:
    NlcApp()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
