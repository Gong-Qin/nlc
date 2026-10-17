#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NLC 自然语言命令行 —— 原生 Windows 图形界面（tkinter）。

为什么用 tkinter（而不是沿用 Linux 版的 GTK3）：
    GTK3 在 Windows 上要额外装一套运行库才跑得起来 —— 那会直接破坏这个项目的
    底线「**零第三方依赖、解压即用**」。tkinter 是 Python 官方安装包自带的标准库，
    不需要装任何东西，双击就能开窗。代价是外观朴素些，靠配色与留白来补。

    想在 Linux 上用原来那套 GTK3 界面：`nlc gui --gtk`（见 gui_gtk3.py）。

★ 本界面里**没有任何一处会自动执行命令** —— 所有执行都必须由用户按下按钮。
  这是设计总纲第一条「AI 只翻译，不执行」在代码里的体现。

tkinter 上踩过的坑（改界面照做）：
    1. **ttk 控件默认主题改不了颜色** ⇒ 必须 `style.theme_use("clam")`，
       否则 Combobox / Scrollbar 永远是系统灰，和配色对不上。
    2. **`tk.Button` 的 3D 边框** ⇒ 必须同时给 `relief="flat"`、`bd=0`、
       `highlightthickness=0`，否则深色配色下会出现难看的白边。
    3. **网络调用放主线程会冻住界面** ⇒ 全部丢进工作线程，
       用 `root.after(0, cb)` 回主线程（注意用 lambda 包住参数）。
    4. **tkinter 没有流式容器**（没有 GTK 的 FlowBox）⇒ 自己按字体量宽度折行，
       见 `TokenFlow`。
    5. **高 DPI 屏上默认是糊的** ⇒ 启动时先调 `SetProcessDpiAwareness`。
    6. **`place()` 叠放各页比 `pack/pack_forget` 更省事**：页面用
       `place(relwidth=1, relheight=1)` 全部铺满，切换时只 `lift()` 对应的那个。
"""

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

# ── 让它既能 `python -m nlc.gui`，也能 `python /path/gui.py` 直接跑 ──
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nlc.blacklist import Blacklist                                      # noqa: E402
from nlc.platform import (IS_WINDOWS, active_shell, launcher_dir,        # noqa: E402
                          shell_label)
from nlc.prompts import build_system_prompt, get_profile, load_profiles  # noqa: E402
from nlc.providers import (OFFLINE, fallback_chain, get_api_key,         # noqa: E402
                           get_provider, load_config, save_config)
from nlc.risk import ALLOW, BLOCK, CONFIRM                               # noqa: E402
from nlc.translator import compose, lookup, regenerate, translate        # noqa: E402
from nlc.wordbank import WordBank, WordBankError                         # noqa: E402

APP_TITLE = "NLC · 自然语言命令行"

LEVEL_TEXT = {ALLOW: "可执行", CONFIRM: "需二次确认", BLOCK: "已禁止"}
LEVEL_ICON = {ALLOW: "✔", CONFIRM: "▲", BLOCK: "✕"}


# --------------------------------------------------------------------------- #
# 配色（与原型同一套；白天/夜晚两套，都按无障碍规范取色）
# --------------------------------------------------------------------------- #
LIGHT = dict(
    page="#F7F7F8", surface="#FFFFFF", elevated="#F3F3F4", hover="#EBEBED",
    text1="#16181C", text2="#5A5D63", text3="#8C8F96",
    border="#E3E4E7", accent="#1E63A8", ok="#1D9E75", danger="#D93F3E", warn="#B0740F",
    on_accent="#FFFFFF",
)
DARK = dict(
    page="#111318", surface="#181B21", elevated="#20242B", hover="#282D36",
    text1="#E7E9ED", text2="#9DA2AC", text3="#6E7481",
    border="#2A2F38", accent="#7CB2EA", ok="#4DBF97", danger="#EE7371", warn="#DCA34E",
    on_accent="#10131A",
)

#: 两套配色里"同一个语义位"的颜色集合 —— 换主题时用它判断这个控件的当前颜色
#: 属于哪一类，从而实现"按语义重刷"而不是"记住每个控件"。
_SURFACE_TOKENS = {v for v in (LIGHT["surface"], DARK["surface"])}
_PAGE_TOKENS = {v for v in (LIGHT["page"], DARK["page"])}
_ELEVATED_TOKENS = {v for v in (LIGHT["elevated"], DARK["elevated"])}
_HOVER_TOKENS = {v for v in (LIGHT["hover"], DARK["hover"])}
_TEXT1_TOKENS = {v for v in (LIGHT["text1"], DARK["text1"])}
_TEXT2_TOKENS = {v for v in (LIGHT["text2"], DARK["text2"],
                             LIGHT["text3"], DARK["text3"])}
_ACCENT_TOKENS = {v for v in (LIGHT["accent"], DARK["accent"])}
_WARN_TOKENS = {v for v in (LIGHT["warn"], DARK["warn"])}
_DANGER_TOKENS = {v for v in (LIGHT["danger"], DARK["danger"])}


def pick_fonts() -> dict:
    """挑一套本机装得上的中文字体。

    为什么不能写死：不同 Windows 上装的中文字体不一样，写死会掉回 tkinter 的
    默认字体（中文 Windows 上常常渲染成方框或极窄字形）。
    """
    families = set(tkfont.families())
    ui = next((f for f in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
                           "PingFang SC", "Noto Sans CJK SC", "文泉驿微米黑",
                           "SimHei", "SimSun", "DejaVu Sans")
               if f in families), "")
    mono = next((f for f in ("Consolas", "Cascadia Mono", "Menlo",
                             "DejaVu Sans Mono", "Courier New")
                 if f in families), "Courier")
    return {"ui": ui, "mono": mono}


# --------------------------------------------------------------------------- #
# 小控件
# --------------------------------------------------------------------------- #
class FlatButton(tk.Button):
    """扁平按钮。tk.Button 默认带一圈 3D 边框，深色配色下很难看。"""

    KINDS = ("normal", "primary", "ok", "danger", "off", "ghost")

    def __init__(self, master, text="", command=None, colors=None, font=None,
                 kind="normal", **kw):
        self._kind = kind
        # padx / pady 给默认值，但允许调用方覆盖 —— 直接写死在 super() 里
        # 会让 `FlatButton(..., padx=14)` 报 "multiple values for keyword argument"
        padx = kw.pop("padx", 12)
        pady = kw.pop("pady", 6)
        super().__init__(master, text=text, command=command, font=font,
                         relief="flat", bd=0, highlightthickness=0,
                         cursor="hand2", padx=padx, pady=pady, **kw)
        self.restyle(colors or LIGHT, kind)
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))

    def _palette(self, colors, kind):
        c = colors
        if kind == "primary":
            return c["accent"], c["on_accent"]
        if kind == "ok":
            return c["ok"], c["on_accent"]
        if kind == "danger":
            return c["danger"], c["on_accent"]
        if kind == "off":
            return c["hover"], c["text3"]
        if kind == "ghost":
            return c["surface"], c["text2"]
        return c["elevated"], c["text1"]

    def restyle(self, colors, kind=None) -> None:
        kind = kind or self._kind
        self._kind = kind
        self._colors = colors
        bg, fg = self._palette(colors, kind)
        self.configure(bg=bg, fg=fg, activebackground=colors["hover"],
                       activeforeground=fg)

    def _hover(self, on: bool) -> None:
        c = self._colors
        kind = self._kind
        try:
            if kind in ("primary", "ok", "danger"):
                return                                   # 强调按钮不做 hover 变色
            self.configure(bg=c["hover"] if on else self._palette(c, kind)[0])
        except tk.TclError:
            pass

    def set_kind(self, colors, kind: str) -> None:
        self.restyle(colors, kind)


class TokenFlow(tk.Frame):
    """词块流式布局：一行放不下就自动换行。

    tkinter 没有现成的流式容器（没有 GTK 的 FlowBox），所以这里自己算：
    用字体量出每个词块按钮的宽度，累加超过可用宽度就换一行。
    ★ 「视觉粘连」在这里落地：与前一块紧贴时只留 1px 缝（其它是 7px），
      既不拆碎观感，每一个又都能单独点掉。
    """

    def __init__(self, master, colors, font, **kw):
        super().__init__(master, **kw)
        self.colors = colors
        self.font = font
        self._rows: list = []

    def clear(self) -> None:
        for r in self._rows:
            try:
                r.destroy()
            except tk.TclError:
                pass
        self._rows = []

    def _new_row(self):
        row = tk.Frame(self, bg=self.colors["surface"])
        row.pack(anchor="w", fill="x")
        self._rows.append(row)
        return row

    def add_tokens(self, blocks, killed, on_click) -> None:
        c = self.colors
        self.clear()
        self.configure(bg=c["surface"])
        avail = max(240, self.winfo_width() or 720)
        row = self._new_row()
        used = 0
        for b in blocks:
            pad = 1 if not b.gap_before else 7
            w = self.font.measure(b.text or " ") + 30 + pad
            if used + w > avail and used > 0:
                row = self._new_row()
                used = 0
            kind = "danger" if b.index in killed else "normal"
            btn = FlatButton(row, text=b.text, colors=c, font=self.font, kind=kind,
                             command=lambda i=b.index: on_click(i))
            btn.pack(side="left", padx=(pad, 0), pady=3)
            used += w


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
        self._built = False
        self._current_nav = "cmd"
        self.sc_empty = None
        self.c = LIGHT

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("1200x840")
        self.root.minsize(960, 660)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        fams = pick_fonts()
        self.f_ui = tkfont.Font(family=fams["ui"], size=10)
        self.f_ui_b = tkfont.Font(family=fams["ui"], size=10, weight="bold")
        self.f_h1 = tkfont.Font(family=fams["ui"], size=13, weight="bold")
        self.f_small = tkfont.Font(family=fams["ui"], size=9)
        self.f_mono = tkfont.Font(family=fams["mono"], size=12)
        self.f_mono_s = tkfont.Font(family=fams["mono"], size=10)

        self.style = ttk.Style()
        try:
            # ★ 必须换成 clam：默认 vista 主题下 Combobox / Scrollbar 颜色改不动
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        self._build()
        self.apply_theme(self.mode, save=False)
        self.refresh_provider_label()
        self.refresh_blacklist()
        self.refresh_wordbank()
        self.render_result(None)
        self._built = True

        if os.environ.get("NLC_GUI_DEMO"):
            self.root.after(900, self._demo_kick)

    # ------------------------------------------------------------------ #
    # 组装
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        c = LIGHT

        hdr = tk.Frame(self.root, bg=c["surface"], height=66)
        hdr.pack(side="top", fill="x")
        hdr.pack_propagate(False)
        tk.Frame(self.root, bg=c["border"], height=1).pack(side="top", fill="x")

        tk.Label(hdr, text="▍", bg=c["surface"], fg=c["accent"],
                 font=self.f_h1).pack(side="left", padx=(14, 4))
        tb = tk.Frame(hdr, bg=c["surface"])
        tb.pack(side="left")
        tk.Label(tb, text="NLC", bg=c["surface"], fg=c["text1"],
                 font=self.f_h1, anchor="w").pack(anchor="w")
        tk.Label(tb, text="自然语言命令行 · AI 只翻译，不执行",
                 bg=c["surface"], fg=c["text3"], font=self.f_small,
                 anchor="w").pack(anchor="w")

        # 右侧控件（从右往左 pack）
        self.gear_btn = FlatButton(hdr, text="⚙ 设置", colors=c, font=self.f_ui,
                                   kind="ghost", command=self.on_settings)
        self.gear_btn.pack(side="right", padx=(6, 14), pady=14)
        self.theme_btn = FlatButton(hdr, text="", colors=c, font=self.f_ui,
                                    kind="ghost", command=self.on_theme)
        self.theme_btn.pack(side="right", padx=6, pady=14)
        self.prov_lbl = tk.Label(hdr, text="", bg=c["surface"], fg=c["text3"],
                                 font=self.f_small)
        self.prov_lbl.pack(side="right", padx=10)

        self.profiles = load_profiles()
        cur = (self.cfg.get("ui") or {}).get("prompt_profile", "concise")
        self._prof_ids = list(self.profiles.keys())
        labels = ["%s %s" % (self.profiles[p].emoji, self.profiles[p].name)
                  for p in self._prof_ids]
        tk.Label(hdr, text="角色卡", bg=c["surface"], fg=c["text2"],
                 font=self.f_small).pack(side="right", padx=(10, 4))
        self.card_combo = ttk.Combobox(hdr, values=labels, state="readonly",
                                       width=12, font=self.f_ui)
        self.card_combo.current(self._prof_ids.index(cur) if cur in self._prof_ids else 0)
        self.card_combo.pack(side="right", pady=18)
        self.card_combo.bind("<<ComboboxSelected>>", self.on_card_changed)

        body = tk.Frame(self.root, bg=c["page"])
        body.pack(side="top", fill="both", expand=True)

        nav = tk.Frame(body, bg=c["surface"], width=168)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        tk.Frame(body, bg=c["border"], width=1).pack(side="left", fill="y")

        self.nav_btns = {}
        for key, label in (("cmd", "命令行"), ("lookup", "查词"), ("script", "对话式脚本"),
                           ("words", "词库"), ("blk", "黑名单")):
            b = FlatButton(nav, text=label, colors=c, font=self.f_ui, kind="ghost",
                           anchor="w", padx=14, command=lambda k=key: self.nav_select(k))
            b.pack(fill="x", padx=10, pady=3)
            self.nav_btns[key] = b

        self.stack_host = tk.Frame(body, bg=c["page"])
        self.stack_host.pack(side="left", fill="both", expand=True)

        self.pages = {
            "cmd": self._page_cmd(self.stack_host),
            "lookup": self._page_lookup(self.stack_host),
            "script": self._page_script(self.stack_host),
            "words": self._page_words(self.stack_host),
            "blk": self._page_blk(self.stack_host),
        }
        # ★ 各页叠着放，切换时只 lift 对应的那个 —— 比反复 pack_forget 省事
        for p in self.pages.values():
            p.place(x=0, y=0, relwidth=1, relheight=1)
        self.nav_select("cmd")

    # ------------------------------------------------------------------ #
    # 通用小构件
    # ------------------------------------------------------------------ #
    def card(self, master, pad=14) -> tk.Frame:
        """带 1px 描边的卡片，返回可往里放东西的内层容器。"""
        c = self.c
        outer = tk.Frame(master, bg=c["border"])
        outer.pack(fill="x", padx=14, pady=(12, 0))
        inner = tk.Frame(outer, bg=c["surface"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        holder = tk.Frame(inner, bg=c["surface"])
        holder.pack(fill="both", expand=True, padx=pad, pady=pad)
        return holder

    def label(self, master, text="", font=None, fg=None, **kw) -> tk.Label:
        c = self.c
        return tk.Label(master, text=text, font=font or self.f_ui,
                        fg=fg or c["text1"], bg=c["surface"],
                        justify="left", anchor="w", **kw)

    def wrap_label(self, master, text="", font=None, fg=None, width=760, **kw) -> tk.Label:
        lab = self.label(master, text, font=font, fg=fg, **kw)
        lab.configure(wraplength=width)
        return lab

    def entry(self, master, width=None) -> tk.Entry:
        c = self.c
        e = tk.Entry(master, font=self.f_ui, bg=c["elevated"], fg=c["text1"],
                     insertbackground=c["text1"], relief="flat", bd=0,
                     highlightthickness=1, highlightbackground=c["border"],
                     highlightcolor=c["accent"])
        if width:
            e.configure(width=width)
        return e

    def scroll_area(self, master) -> tk.Frame:
        """返回一个会滚动的容器：往它里面 pack 东西即可。"""
        c = self.c
        host = tk.Frame(master, bg=c["page"])
        host.pack(fill="both", expand=True)
        canvas = tk.Canvas(host, bg=c["page"], highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(host, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=c["page"])
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        def _resize(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(win, width=canvas.winfo_width())

        inner.bind("<Configure>", _resize)
        canvas.bind("<Configure>", _resize)
        canvas.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        return inner

    # ------------------------------------------------------------------ #
    # 页面：命令行
    # ------------------------------------------------------------------ #
    def _page_cmd(self, master) -> tk.Frame:
        c = self.c
        page = tk.Frame(master, bg=c["page"])
        scroll = self.scroll_area(page)

        box = self.card(scroll)
        self.label(box, "用一句话说明你想干什么", font=self.f_small,
                   fg=c["text3"]).pack(anchor="w")
        row = tk.Frame(box, bg=c["surface"])
        row.pack(fill="x", pady=(6, 0))
        self.cmd_entry = self.entry(row)
        self.cmd_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.cmd_entry.insert(0, "找出当前目录下占用空间最大的十个文件夹")
        self.cmd_entry.bind("<Return>", lambda _e: self.do_translate())
        FlatButton(row, text="翻译", colors=c, font=self.f_ui_b, kind="primary",
                   command=self.do_translate).pack(side="left", padx=(8, 0))

        chips = tk.Frame(box, bg=c["surface"])
        chips.pack(fill="x", pady=(8, 0))
        for s in ("看看谁占着 8080 端口", "统计日志里错误出现了几次", "磁盘占用"):
            FlatButton(chips, text=s, colors=c, font=self.f_small,
                       command=lambda t=s: self._set_entry(self.cmd_entry, t)
                       ).pack(side="left", padx=(0, 6))

        res = self.card(scroll)
        head = tk.Frame(res, bg=c["surface"])
        head.pack(fill="x")
        self.cmd_badge = tk.Label(head, text="—", font=self.f_ui_b,
                                  bg=c["hover"], fg=c["text3"], padx=12, pady=4)
        self.cmd_badge.pack(side="left")
        self.cmd_title = self.wrap_label(head, "点掉不对的词块，再点「重新生成」",
                                         font=self.f_small, fg=c["text2"], width=520)
        self.cmd_title.pack(side="left", padx=10, fill="x", expand=True)

        self.tok_host = tk.Frame(res, bg=c["surface"])
        self.tok_host.pack(fill="x", pady=(10, 4))
        self.tok_flow = TokenFlow(self.tok_host, colors=c, font=self.f_mono,
                                  bg=c["surface"])
        self.tok_flow.pack(fill="x")

        self.sum_lbl = self.wrap_label(res, "", font=self.f_mono_s, fg=c["text1"])
        self.sum_lbl.pack(fill="x", pady=(4, 0))

        self.exp_host = tk.Frame(res, bg=c["surface"])
        self.exp_host.pack(fill="x", pady=(6, 0))

        self.notes_lbl = self.wrap_label(res, "", font=self.f_small, fg=c["warn"])
        self.notes_lbl.pack(fill="x", pady=(4, 0))

        acts = tk.Frame(res, bg=c["surface"])
        acts.pack(fill="x", pady=(10, 0))
        self.run_btn = FlatButton(acts, text="执行", colors=c, font=self.f_ui_b,
                                  kind="off", state="disabled", command=self.on_execute)
        self.run_btn.pack(side="left")
        FlatButton(acts, text="重新生成", colors=c, font=self.f_ui,
                   command=self.do_regenerate).pack(side="left", padx=6)
        FlatButton(acts, text="复制命令", colors=c, font=self.f_ui,
                   command=self.on_copy).pack(side="left")

        self.blkbar = tk.Frame(res, bg=c["surface"])
        self.blkbar_lbl = self.wrap_label(self.blkbar, "", font=self.f_small,
                                          fg=c["text3"], width=560)
        self.blkbar_lbl.pack(side="left", fill="x", expand=True)
        FlatButton(self.blkbar, text="升级到永久区", colors=c, font=self.f_small,
                   command=self.on_promote).pack(side="right")

        tk.Frame(scroll, bg=c["page"], height=18).pack(fill="x")
        return page

    def _set_entry(self, entry: tk.Entry, text: str) -> None:
        entry.delete(0, "end")
        entry.insert(0, text)

    # ------------------------------------------------------------------ #
    # 页面：查词
    # ------------------------------------------------------------------ #
    def _page_lookup(self, master) -> tk.Frame:
        c = self.c
        page = tk.Frame(master, bg=c["page"])
        scroll = self.scroll_area(page)

        box = self.card(scroll)
        self.label(box, "输入一个词，看它对应哪个命令词块", font=self.f_small,
                   fg=c["text3"]).pack(anchor="w")
        row = tk.Frame(box, bg=c["surface"])
        row.pack(fill="x", pady=(6, 0))
        self.lk_entry = self.entry(row)
        self.lk_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.lk_entry.insert(0, "查端口占用")
        self.lk_entry.bind("<Return>", lambda _e: self.do_lookup())
        FlatButton(row, text="查词", colors=c, font=self.f_ui_b, kind="primary",
                   command=self.do_lookup).pack(side="left", padx=(8, 0))

        rc = self.card(scroll)
        self.lk_token = self.label(rc, "—", font=self.f_mono, fg=c["accent"])
        self.lk_token.pack(anchor="w")
        self.lk_sum = self.wrap_label(rc, "", font=self.f_ui)
        self.lk_sum.pack(fill="x", pady=(8, 0))
        self.lk_notes = self.wrap_label(rc, "", font=self.f_small, fg=c["text2"])
        self.lk_notes.pack(fill="x", pady=(4, 0))
        self.wrap_label(
            rc, "查词是「一个词 → 一个命令词块」。它不追求精准翻译，"
                "只求把「这个词是干什么的」讲清楚，让你自己判断。",
            font=self.f_small, fg=c["text3"]).pack(fill="x", pady=(12, 0))
        tk.Frame(scroll, bg=c["page"], height=18).pack(fill="x")
        return page

    # ------------------------------------------------------------------ #
    # 页面：对话式脚本
    # ------------------------------------------------------------------ #
    def _page_script(self, master) -> tk.Frame:
        c = self.c
        page = tk.Frame(master, bg=c["page"])
        scroll = self.scroll_area(page)

        box = self.card(scroll)
        self.label(box, "把用过的命令拼成一个脚本一起跑（不要求逻辑连贯）",
                   font=self.f_small, fg=c["text3"]).pack(anchor="w")
        self.sc_list = tk.Frame(box, bg=c["surface"])
        self.sc_list.pack(fill="x", pady=(8, 0))
        self.sc_empty = self.label(self.sc_list, "（还没有添加任何命令）",
                                   font=self.f_small, fg=c["text3"])
        self.sc_empty.pack(anchor="w")

        row = tk.Frame(box, bg=c["surface"])
        row.pack(fill="x", pady=(10, 0))
        FlatButton(row, text="把当前命令行加进来", colors=c, font=self.f_ui,
                   command=self.on_add_to_script).pack(side="left")
        FlatButton(row, text="组合", colors=c, font=self.f_ui_b, kind="ok",
                   command=self.do_compose).pack(side="left", padx=6)
        FlatButton(row, text="清空", colors=c, font=self.f_ui,
                   command=self.on_clear_script).pack(side="left")

        self.sc_out = self.wrap_label(box, "", font=self.f_mono_s, fg=c["text1"])
        self.sc_out.pack(fill="x", pady=(12, 0))
        tk.Frame(scroll, bg=c["page"], height=18).pack(fill="x")
        return page

    # ------------------------------------------------------------------ #
    # 页面：词库
    # ------------------------------------------------------------------ #
    def _page_words(self, master) -> tk.Frame:
        c = self.c
        page = tk.Frame(master, bg=c["page"])
        scroll = self.scroll_area(page)

        box = self.card(scroll)
        self.tr_var = tk.BooleanVar(
            value=bool((self.cfg.get("ui") or {}).get("translate_enabled", True)))
        ttk.Checkbutton(box, text="翻译功能", variable=self.tr_var,
                        command=self.on_trans_switch).pack(anchor="w")
        self.tr_sub = self.wrap_label(box, "", font=self.f_small, fg=c["text2"])
        self.tr_sub.pack(fill="x", pady=(4, 0))

        box2 = self.card(scroll)
        self.label(box2, "我的词库（一个自然语言词只对应一个命令词块；重名会报错，不覆盖）",
                   font=self.f_small, fg=c["text3"]).pack(anchor="w")
        self.wb_list = tk.Frame(box2, bg=c["surface"])
        self.wb_list.pack(fill="x", pady=(8, 0))

        addrow = tk.Frame(box2, bg=c["surface"])
        addrow.pack(fill="x", pady=(10, 0))
        self.wb_k = self.entry(addrow)
        self.wb_k.pack(side="left", fill="x", expand=True, ipady=5)
        self.wb_v = self.entry(addrow)
        self.wb_v.pack(side="left", fill="x", expand=True, ipady=5, padx=6)
        FlatButton(addrow, text="添加", colors=c, font=self.f_ui,
                   command=self.on_wb_add).pack(side="left")
        FlatButton(addrow, text="生成分享码", colors=c, font=self.f_ui,
                   command=self.on_share_code).pack(side="left", padx=(6, 0))
        self._ph(self.wb_k, "自然语言词，如「看磁盘」")
        self._ph(self.wb_v, "命令词块，如「dir」")

        self.wb_msg = self.wrap_label(box2, "", font=self.f_small, fg=c["text2"])
        self.wb_msg.pack(fill="x", pady=(8, 0))
        self.update_trans_text()
        tk.Frame(scroll, bg=c["page"], height=18).pack(fill="x")
        return page

    def _ph(self, entry: tk.Entry, text: str) -> None:
        """tkinter 原生没有 placeholder，自己用水印文字模拟。"""
        c = self.c

        def on_in(_e):
            if entry.cget("fg") == c["text3"]:
                entry.delete(0, "end")
                entry.configure(fg=c["text1"])

        def on_out(_e):
            if not entry.get().strip():
                entry.delete(0, "end")
                entry.insert(0, text)
                entry.configure(fg=c["text3"])

        entry.insert(0, text)
        entry.configure(fg=c["text3"])
        entry.bind("<FocusIn>", on_in)
        entry.bind("<FocusOut>", on_out)

    def _ph_value(self, entry: tk.Entry) -> str:
        return "" if entry.cget("fg") == self.c["text3"] else entry.get().strip()

    # ------------------------------------------------------------------ #
    # 页面：黑名单
    # ------------------------------------------------------------------ #
    def _page_blk(self, master) -> tk.Frame:
        c = self.c
        page = tk.Frame(master, bg=c["page"])
        wrap = tk.Frame(page, bg=c["page"])
        wrap.pack(fill="both", expand=True, padx=14, pady=12)

        left = tk.Frame(wrap, bg=c["border"])
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(wrap, bg=c["border"])
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        a = tk.Frame(left, bg=c["surface"])
        a.pack(fill="both", expand=True, padx=1, pady=1)
        a_in = tk.Frame(a, bg=c["surface"])
        a_in.pack(fill="both", expand=True, padx=14, pady=14)
        self.label(a_in, "临时区（按【下标】记 · 新查询自动清空）",
                   font=self.f_ui_b).pack(anchor="w")
        self.blk_session = tk.Frame(a_in, bg=c["surface"])
        self.blk_session.pack(fill="both", expand=True, pady=(8, 0))
        FlatButton(a_in, text="全部升级到永久区", colors=c, font=self.f_small,
                   command=self.on_promote).pack(anchor="w", pady=(8, 0))
        self.wrap_label(
            a_in, "为什么按下标记：一次查询里 | 可能出现两次，只有下标能分清你点的是哪一个。",
            font=self.f_small, fg=c["text3"]).pack(fill="x", pady=(6, 0))

        b = tk.Frame(right, bg=c["surface"])
        b.pack(fill="both", expand=True, padx=1, pady=1)
        b_in = tk.Frame(b, bg=c["surface"])
        b_in.pack(fill="both", expand=True, padx=14, pady=14)
        self.label(b_in, "永久区（按【词块文本】记 · 跨会话生效）",
                   font=self.f_ui_b).pack(anchor="w")
        self.blk_perm = tk.Frame(b_in, bg=c["surface"])
        self.blk_perm.pack(fill="both", expand=True, pady=(8, 0))
        self.wrap_label(
            b_in, "为什么按文本记：跨命令时「第 8 个词块」毫无意义，只有文本能跨命令生效。",
            font=self.f_small, fg=c["text3"]).pack(fill="x", pady=(6, 0))
        return page

    # ------------------------------------------------------------------ #
    # 主题
    # ------------------------------------------------------------------ #
    def apply_theme(self, mode: str, save: bool = True) -> None:
        self.mode = mode
        c = DARK if mode == "night" else LIGHT
        self.c = c
        self.root.configure(bg=c["page"])

        self._repaint(self.root, c)
        self._style_theme(c)

        self.theme_btn.configure(text="☀ 白天" if mode == "night" else "🌙 夜晚")
        self.cfg.setdefault("ui", {})["theme"] = mode
        if save:
            save_config(self.cfg)

        # 这些控件的内容依赖配色，交给各自的重绘函数
        self.nav_select(self._current_nav)
        self.refresh_provider_label()
        self.refresh_blacklist()
        self.refresh_wordbank()
        if self.base_tr is not None:
            self.render_result(self.base_tr)
        else:
            self.render_result(None)

    def _repaint(self, w, c) -> None:
        """按语义重刷一棵控件树的配色。

        做法：看控件当前的 bg/fg 落在哪个"语义位"的颜色集合里，
        就把它换成新配色里同一个语义位的颜色。这样不用给每个控件登记。
        """
        try:
            if isinstance(w, FlatButton):
                w.restyle(c)
            elif isinstance(w, tk.Label):
                bg = str(w.cget("bg"))
                fg = str(w.cget("fg"))
                newbg = (c["surface"] if bg in _SURFACE_TOKENS else
                         c["page"] if bg in _PAGE_TOKENS else
                         c["elevated"] if bg in _ELEVATED_TOKENS else
                         c["hover"] if bg in _HOVER_TOKENS else None)
                newfg = (c["text1"] if fg in _TEXT1_TOKENS else
                         c["text2"] if fg in _TEXT2_TOKENS else
                         c["accent"] if fg in _ACCENT_TOKENS else
                         c["warn"] if fg in _WARN_TOKENS else
                         c["danger"] if fg in _DANGER_TOKENS else None)
                kw = {}
                if newbg:
                    kw["bg"] = newbg
                if newfg:
                    kw["fg"] = newfg
                if kw:
                    w.configure(**kw)
            elif isinstance(w, (tk.Entry, tk.Text)):
                w.configure(bg=c["elevated"], fg=c["text1"],
                            insertbackground=c["text1"],
                            highlightbackground=c["border"],
                            highlightcolor=c["accent"])
            elif isinstance(w, (tk.Frame, tk.Canvas)):
                cls = w.__class__.__name__
                if cls == "Toplevel":
                    pass
                else:
                    bg = str(w.cget("bg"))
                    if bg in _SURFACE_TOKENS:
                        w.configure(bg=c["surface"])
                    elif bg in _PAGE_TOKENS:
                        w.configure(bg=c["page"])
                    elif bg in _ELEVATED_TOKENS:
                        w.configure(bg=c["elevated"])
                    elif bg in _HOVER_TOKENS:
                        w.configure(bg=c["hover"])
                    elif bg in {LIGHT["border"], DARK["border"]}:
                        w.configure(bg=c["border"])
                    elif cls == "Canvas":
                        w.configure(bg=c["page"])
        except tk.TclError:
            pass
        for ch in w.winfo_children():
            self._repaint(ch, c)

    def _style_theme(self, c) -> None:
        self.style.configure("TCombobox", fieldbackground=c["elevated"],
                             background=c["elevated"], foreground=c["text1"],
                             arrowcolor=c["text2"], bordercolor=c["border"],
                             lightcolor=c["border"], darkcolor=c["border"],
                             selectbackground=c["accent"], selectforeground=c["on_accent"])
        self.style.map("TCombobox",
                       fieldbackground=[("readonly", c["elevated"])],
                       foreground=[("readonly", c["text1"])])
        self.style.configure("Vertical.TScrollbar", background=c["elevated"],
                             troughcolor=c["page"], bordercolor=c["page"],
                             arrowcolor=c["text2"], lightcolor=c["elevated"],
                             darkcolor=c["elevated"])
        self.style.configure("TCheckbutton", background=c["surface"],
                             foreground=c["text1"])
        self.style.map("TCheckbutton",
                       background=[("active", c["surface"])],
                       foreground=[("disabled", c["text3"])])

    def on_theme(self) -> None:
        self.apply_theme("night" if self.mode == "day" else "day")

    # ------------------------------------------------------------------ #
    # 导航
    # ------------------------------------------------------------------ #
    def nav_select(self, key: str) -> None:
        self._current_nav = key
        for k, b in self.nav_btns.items():
            b.set_kind(self.c, "primary" if k == key else "ghost")
        if key in self.pages:
            self.pages[key].lift()
        if key == "blk":
            self.refresh_blacklist()
        if key == "words":
            self.refresh_wordbank()

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
        self.prov_lbl.configure(text="● " + (p.name if p else "没有可用通道"))

    def active_profile(self):
        i = self.card_combo.current()
        pid = self._prof_ids[i] if 0 <= i < len(self._prof_ids) else "concise"
        return self.profiles.get(pid) or get_profile("concise")

    def on_card_changed(self, _e=None) -> None:
        if not self._built:
            return
        i = self.card_combo.current()
        pid = self._prof_ids[i] if 0 <= i < len(self._prof_ids) else "concise"
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
        self.cmd_title.configure(text="正在请求模型…", fg=self.c["text2"])

        def worker():
            try:
                r = fn()
            except Exception as exc:                      # noqa: BLE001
                self.root.after(0, lambda e=exc: self._finish(on_fail, e))
            else:
                self.root.after(0, lambda v=r: self._finish(on_done, v))

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, cb, payload) -> None:
        self.busy = False
        try:
            cb(payload)
        except Exception as exc:                          # noqa: BLE001
            self.cmd_title.configure(text="出错了：%s" % exc, fg=self.c["danger"])

    def _need_provider(self) -> bool:
        if not self.pick_provider():
            self.cmd_title.configure(
                text="没有可用的 AI 通道 —— 请在右上角「设置」里填 API Key",
                fg=self.c["danger"])
            return False
        return True

    # ------------------------------------------------------------------ #
    # 命令行页
    # ------------------------------------------------------------------ #
    def do_translate(self) -> None:
        if not self._need_provider():
            return
        text = self.cmd_entry.get().strip()
        if not text:
            return
        self.killed = []
        self.bl.start_query(text)
        prof = self.active_profile()
        chain = self.bl.exclude_texts()

        def job():
            return translate(text, self.provider, exclude_texts=chain, profile=prof)

        self.run_async(job, self.render_result,
                       lambda e: self.cmd_title.configure(text="翻译失败：" + str(e),
                                                          fg=self.c["danger"]))

    def do_regenerate(self) -> None:
        if not self._need_provider() or self.base_tr is None:
            return
        text = self.cmd_entry.get().strip()
        prof = self.active_profile()
        killed = list(self.killed)
        base = self.base_tr
        chain = self.bl.exclude_texts()

        def job():
            return regenerate(text, base, killed, self.provider,
                              exclude_texts=chain, profile=prof)

        self.killed = []
        self.run_async(job, self.render_result,
                       lambda e: self.cmd_title.configure(text="重新生成失败：" + str(e),
                                                          fg=self.c["danger"]))

    def do_lookup(self) -> None:
        if not self._need_provider():
            return
        term = self.lk_entry.get().strip()
        if not term:
            return
        prof = self.active_profile()
        self.lk_sum.configure(text="正在查…")

        def job():
            return lookup(term, self.provider, profile=prof)

        def done(tr):
            self.lk_token.configure(text=tr.command, fg=self.c["accent"])
            self.lk_sum.configure(text=tr.summary or "")
            self.lk_notes.configure(
                text=(tr.blocks[0].meaning if tr.blocks else "") + "\n" + (tr.notes or ""))

        self.run_async(job, done,
                       lambda e: self.lk_sum.configure(text="查词失败：" + str(e)))

    def render_result(self, tr) -> None:
        c = self.c
        self.tok_flow.colors = c
        self.tok_flow.configure(bg=c["surface"])

        if tr is None:
            self.base_tr = None
            self.cmd_badge.configure(text="—", bg=c["hover"], fg=c["text3"])
            self.cmd_title.configure(text="点掉不对的词块，再点「重新生成」", fg=c["text2"])
            self.tok_flow.clear()
            self.sum_lbl.configure(text="")
            self.notes_lbl.configure(text="")
            for ch in self.exp_host.winfo_children():
                ch.destroy()
            self.run_btn.set_kind(c, "off")
            self.run_btn.configure(text="执行", state="disabled")
            self.blkbar.pack_forget()
            return

        self.base_tr = tr
        self.sum_lbl.configure(text=tr.command)
        self.notes_lbl.configure(text=tr.notes or "")

        a = tr.risk
        if a:
            self.cmd_badge.configure(
                text="%s %s" % (LEVEL_ICON.get(a.level, "?"),
                                LEVEL_TEXT.get(a.level, a.level)),
                bg=(c["ok"] if a.level == ALLOW else
                    c["danger"] if a.level == CONFIRM else c["hover"]),
                fg=(c["on_accent"] if a.level in (ALLOW, CONFIRM) else c["text3"]))
            self.cmd_title.configure(text=a.reason,
                                     fg=c["text2"] if a.level == ALLOW else c["text1"])
            ok = a.executable
        else:
            self.cmd_badge.configure(text="—", bg=c["hover"], fg=c["text3"])
            ok = False

        kind = ("ok" if (a and a.level == ALLOW)
                else "danger" if (a and a.level == CONFIRM) else "off")
        self.run_btn.set_kind(c, kind)
        self.run_btn.configure(
            text=("执行" if (a and a.level == ALLOW)
                  else "执行（需确认）" if (a and a.level == CONFIRM)
                  else "已禁止，不可执行"),
            state="normal" if ok else "disabled")

        # ★ 量宽度前必须先布局，否则 winfo_width() 会是 1
        self.root.update_idletasks()
        self.tok_flow.add_tokens(tr.blocks, set(self.killed), self.on_token_clicked)

        for ch in self.exp_host.winfo_children():
            ch.destroy()
        for b in tr.blocks:
            line = tk.Frame(self.exp_host, bg=c["surface"])
            line.pack(fill="x", pady=1)
            tk.Label(line, text=b.text, font=self.f_mono_s, fg=c["accent"],
                     bg=c["surface"], width=12, anchor="e").pack(side="left")
            lab = tk.Label(line, text=b.meaning or "", font=self.f_small,
                           fg=c["text2"], bg=c["surface"], justify="left", anchor="w")
            lab.configure(wraplength=660)
            lab.pack(side="left", fill="x", expand=True, padx=(10, 0))

        self.render_blkbar()

    def on_token_clicked(self, index: int) -> None:
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
            self.blkbar.pack_forget()
            return
        names = []
        for i in sorted(self.killed):
            b = next((x for x in self.base_tr.blocks if x.index == i), None)
            if b:
                names.append(b.text)
        self.blkbar_lbl.configure(
            text="临时区已拉黑 %d 个词块：%s —— 重新生成时会保留其余、只替换这些"
                 % (len(names), " ".join(names)))
        self.blkbar.pack(fill="x", pady=(8, 0))

    def on_promote(self) -> None:
        moved = self.bl.promote()
        self.killed = []
        if self.base_tr is not None:
            self.render_result(self.base_tr)
        self.refresh_blacklist()
        self.cmd_title.configure(
            text="已升级到永久区：%s" % (" ".join(moved) if moved else "（没有可升级的）"),
            fg=self.c["text2"])

    def on_execute(self) -> None:
        """★ 执行入口。整个 NLC 只有这里、且只有用户按下按钮，才会真的跑命令。"""
        if self.base_tr is None:
            return
        a = self.base_tr.risk
        if a and a.level == BLOCK:
            return
        if self.ask_execute(self.base_tr.command):
            self.cmd_title.configure(
                text="版本 0.1 尚未接入执行后端（执行权归属设计上单独实现）",
                fg=self.c["warn"])

    def ask_execute(self, command: str) -> bool:
        """危险确认框。**默认焦点落在「取消」上**，回车不会误执行。"""
        c = self.c
        dlg = tk.Toplevel(self.root)
        dlg.title("确认要执行吗")
        dlg.configure(bg=c["surface"])
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        tk.Label(dlg, text="确认要执行吗", font=self.f_h1, bg=c["surface"],
                 fg=c["text1"]).pack(anchor="w", padx=18, pady=(16, 6))
        tk.Label(dlg, text=command, font=self.f_mono_s, bg=c["elevated"],
                 fg=c["text1"], wraplength=520, justify="left").pack(
            fill="x", padx=18, pady=(0, 10), ipady=8)
        tk.Label(dlg, text="NLC 本身不执行任何东西 —— 只有你按下按钮才会执行。",
                 font=self.f_small, bg=c["surface"], fg=c["text2"],
                 wraplength=520, justify="left").pack(anchor="w", padx=18)

        result = {"ok": False}

        def ok():
            result["ok"] = True
            dlg.destroy()

        btns = tk.Frame(dlg, bg=c["surface"])
        btns.pack(fill="x", padx=18, pady=16)
        cancel = FlatButton(btns, text="取消", colors=c, font=self.f_ui,
                            command=dlg.destroy)
        cancel.pack(side="right", padx=(6, 0))
        FlatButton(btns, text="执行", colors=c, font=self.f_ui_b, kind="danger",
                   command=ok).pack(side="right")
        cancel.focus_set()
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        self.root.wait_window(dlg)
        return result["ok"]

    def on_copy(self) -> None:
        if self.base_tr is None:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.base_tr.command)
        self.cmd_title.configure(text="已复制到剪贴板", fg=self.c["text2"])

    def _demo_kick(self) -> None:
        try:
            self.do_translate()
        except Exception:                                 # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # 脚本页
    # ------------------------------------------------------------------ #
    def on_add_to_script(self) -> None:
        c = self.c
        if self.base_tr is None:
            self.cmd_title.configure(text="先在上面翻译出一条命令，再加进来", fg=self.c["text2"])
            return
        if self.sc_empty is not None:
            self.sc_empty.pack_forget()
            self.sc_empty = None
        row = tk.Frame(self.sc_list, bg=c["surface"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text=self.base_tr.command, font=self.f_mono_s, fg=c["text1"],
                 bg=c["surface"], anchor="w", justify="left").pack(
            side="left", fill="x", expand=True)
        FlatButton(row, text="移除", colors=c, font=self.f_small, kind="ghost",
                   command=lambda r=row: r.destroy()).pack(side="right")

    def on_clear_script(self) -> None:
        for ch in self.sc_list.winfo_children():
            ch.destroy()
        self.sc_empty = self.label(self.sc_list, "（还没有添加任何命令）",
                                   font=self.f_small, fg=self.c["text3"])
        self.sc_empty.pack(anchor="w")
        self.sc_out.configure(text="")

    def do_compose(self) -> None:
        if not self._need_provider():
            return
        cmds = [w.cget("text") for ch in self.sc_list.winfo_children()
                for w in ch.winfo_children() if isinstance(w, tk.Label)]
        if not cmds:
            self.sc_out.configure(text="先「把当前命令行加进来」几条")
            return
        prof = self.active_profile()

        def job():
            return compose(cmds, self.provider, profile=prof)

        self.run_async(job, lambda tr: self.sc_out.configure(text=tr.command),
                       lambda e: self.sc_out.configure(text="组合失败：" + str(e)))

    # ------------------------------------------------------------------ #
    # 词库页
    # ------------------------------------------------------------------ #
    def update_trans_text(self) -> None:
        on = bool(self.tr_var.get())
        self.tr_sub.configure(
            text=("走 AI 翻译；词库命中的结果会额外标注「来自个人词库」" if on
                  else "只查词库，不调 AI。回车直接跑对应命令（⚠️ 危险命令仍需确认）"))

    def on_trans_switch(self) -> None:
        self.cfg.setdefault("ui", {})["translate_enabled"] = bool(self.tr_var.get())
        save_config(self.cfg)
        self.update_trans_text()

    def refresh_wordbank(self) -> None:
        c = self.c
        for ch in self.wb_list.winfo_children():
            ch.destroy()
        if not len(self.wb):
            self.label(self.wb_list, "（空）", font=self.f_small,
                       fg=c["text3"]).pack(anchor="w")
            return
        for e in self.wb.all():
            row = tk.Frame(self.wb_list, bg=c["surface"])
            row.pack(fill="x", pady=2)
            tk.Label(row, text=e.term, font=self.f_ui, fg=c["text1"], bg=c["surface"],
                     width=16, anchor="w").pack(side="left")
            tk.Label(row, text=e.token, font=self.f_mono_s, fg=c["accent"],
                     bg=c["surface"], width=14, anchor="w").pack(side="left")
            tk.Label(row, text=("自己填的" if e.verified else "⚠ 未经审核"),
                     font=self.f_small, fg=c["text3"], bg=c["surface"],
                     anchor="w").pack(side="left", fill="x", expand=True)
            FlatButton(row, text="删除", colors=c, font=self.f_small, kind="ghost",
                       command=lambda t=e.term: self.on_wb_remove(t)).pack(side="right")

    def on_wb_add(self) -> None:
        k = self._ph_value(self.wb_k)
        v = self._ph_value(self.wb_v)
        if not k or not v:
            return
        try:
            self.wb.add(k, v)
        except WordBankError as exc:
            self.wb_msg.configure(text=str(exc))
            return
        for e, hint in ((self.wb_k, "自然语言词，如「看磁盘」"),
                        (self.wb_v, "命令词块，如「dir」")):
            e.delete(0, "end")
            e.insert(0, hint)
            e.configure(fg=self.c["text3"])
        self.wb_msg.configure(text="已添加：%s → %s" % (k, v))
        self.refresh_wordbank()

    def on_wb_remove(self, term: str) -> None:
        self.wb.remove(term)
        self.refresh_wordbank()

    def on_share_code(self) -> None:
        code = self.wb.export_code()
        self.root.clipboard_clear()
        self.root.clipboard_append(code)
        self.wb_msg.configure(text="分享码已复制到剪贴板：\n" + code)

    # ------------------------------------------------------------------ #
    # 黑名单页
    # ------------------------------------------------------------------ #
    def refresh_blacklist(self) -> None:
        c = self.c
        for box, items, is_perm in ((self.blk_session, self.bl.session, False),
                                    (self.blk_perm, list(self.bl.permanent.values()), True)):
            for ch in box.winfo_children():
                ch.destroy()
            if not items:
                self.label(box, "（空）", font=self.f_small, fg=c["text3"]).pack(anchor="w")
                continue
            for it in items:
                row = tk.Frame(box, bg=c["surface"])
                row.pack(fill="x", pady=2)
                if is_perm:
                    txt = "%s    %s %s" % (it.text, it.added, it.reason or "")
                    act = FlatButton(row, text="解除", colors=c, font=self.f_small,
                                     kind="ghost",
                                     command=lambda t=it.text: self.on_unban(t))
                else:
                    txt = "[%d] %s" % (it.index, it.text)
                    act = FlatButton(row, text="取消", colors=c, font=self.f_small,
                                     kind="ghost",
                                     command=lambda i=it.index: self.on_unkill(i))
                tk.Label(row, text=txt, font=self.f_small, fg=c["text2"],
                         bg=c["surface"], anchor="w", justify="left").pack(
                    side="left", fill="x", expand=True)
                act.pack(side="right")

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
    def on_settings(self) -> None:
        c = self.c
        dlg = tk.Toplevel(self.root)
        dlg.title("设置")
        dlg.configure(bg=c["surface"])
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.geometry("660x700")

        host = tk.Frame(dlg, bg=c["surface"])
        host.pack(fill="both", expand=True, padx=18, pady=16)

        tk.Label(host, text="运行环境", font=self.f_ui_b, bg=c["surface"],
                 fg=c["text1"]).pack(anchor="w")
        for line in ("平台：%s" % ("Windows" if IS_WINDOWS else "Linux"),
                     "判定的命令行：%s（%s）" % (shell_label(), active_shell()),
                     "界面：tkinter（Python 自带，零第三方依赖）",
                     "安装目录：%s" % launcher_dir()):
            tk.Label(host, text="  " + line, font=self.f_small, bg=c["surface"],
                     fg=c["text2"], anchor="w").pack(fill="x")

        self._sep(host)
        tk.Label(host, text="AI 通道（降级链：云端 → 本地 → 局域网 → 离线兜底）",
                 font=self.f_ui_b, bg=c["surface"], fg=c["text1"]).pack(anchor="w")
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
                    # ★ 这里绝不能调 chan_test()：真实网络请求会把界面冻住好几秒
                    t = "%s —— 已填密钥（通道体检请用命令行 nlc test）" % p.name
            tk.Label(host, text="  " + t, font=self.f_small, bg=c["surface"],
                     fg=c["text2"], anchor="w").pack(fill="x")

        self._sep(host)
        tk.Label(host, text="完全访问模式（对照 Codex 的 danger-full-access）",
                 font=self.f_ui_b, bg=c["surface"], fg=c["text1"]).pack(anchor="w")
        self.danger_var = tk.BooleanVar(
            value=bool((self.cfg.get("ui") or {}).get("danger_full_access", False)))
        row = tk.Frame(host, bg=c["surface"])
        row.pack(fill="x")
        ttk.Checkbutton(row, text="开启", variable=self.danger_var,
                        command=self.on_danger_switch).pack(side="left")
        tk.Label(row, text="开启后「需确认」档不再弹框 —— 但「禁止」档仍然点不动，这一档没有开关。",
                 font=self.f_small, bg=c["surface"], fg=c["text3"],
                 wraplength=420, justify="left", anchor="w").pack(side="left", padx=8)

        self._sep(host)
        tk.Label(host, text="当前角色卡对 translate 的完整系统提示词",
                 font=self.f_ui_b, bg=c["surface"], fg=c["text1"]).pack(anchor="w")
        txt = tk.Text(host, height=15, font=self.f_mono_s, bg=c["elevated"],
                      fg=c["text1"], relief="flat", bd=0, wrap="word",
                      insertbackground=c["text1"], padx=8, pady=8)
        txt.pack(fill="both", expand=True, pady=(6, 0))
        txt.insert("1.0", build_system_prompt(self.active_profile(), "translate"))
        txt.configure(state="disabled")

        FlatButton(host, text="关闭", colors=c, font=self.f_ui, kind="primary",
                   command=dlg.destroy).pack(anchor="e", pady=(12, 0))
        dlg.bind("<Escape>", lambda _e: dlg.destroy())

    def _sep(self, host) -> None:
        tk.Frame(host, bg=self.c["border"], height=1).pack(fill="x", pady=10)

    def on_danger_switch(self) -> None:
        turn_on = bool(self.danger_var.get())
        if turn_on and not self.ask_phrase():
            self.danger_var.set(False)
            return
        self.cfg.setdefault("ui", {})["danger_full_access"] = turn_on
        save_config(self.cfg)
        self.cmd_title.configure(
            text="完全访问模式已%s" % ("开启（危险提示不再弹出）" if turn_on else "关闭"),
            fg=self.c["warn"] if turn_on else self.c["text2"])

    def ask_phrase(self) -> bool:
        """开启完全访问模式必须手动输入确认短语 —— 不允许一键打开。"""
        c = self.c
        dlg = tk.Toplevel(self.root)
        dlg.title("完全访问模式")
        dlg.configure(bg=c["surface"])
        dlg.transient(self.root)
        dlg.grab_set()
        tk.Label(dlg, text="完全访问模式", font=self.f_h1, bg=c["surface"],
                 fg=c["text1"]).pack(anchor="w", padx=18, pady=(16, 6))
        tk.Label(dlg, text="这会跳过所有危险确认。\n请手动输入确认短语「我明白风险」：",
                 font=self.f_ui, bg=c["surface"], fg=c["text2"],
                 justify="left", anchor="w").pack(anchor="w", padx=18)
        e = self.entry(dlg)
        e.pack(fill="x", padx=18, pady=10, ipady=6)
        e.focus_set()
        res = {"ok": False}

        def ok():
            res["ok"] = (e.get().strip() == "我明白风险")
            dlg.destroy()

        btns = tk.Frame(dlg, bg=c["surface"])
        btns.pack(fill="x", padx=18, pady=(0, 16))
        FlatButton(btns, text="取消", colors=c, font=self.f_ui,
                   command=dlg.destroy).pack(side="right", padx=(6, 0))
        FlatButton(btns, text="确定", colors=c, font=self.f_ui_b, kind="primary",
                   command=ok).pack(side="right")
        e.bind("<Return>", lambda _e: ok())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        self.root.wait_window(dlg)
        return res["ok"]

    # ------------------------------------------------------------------ #
    def on_close(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def _enable_dpi_awareness() -> None:
    """不做这一步，Windows 高 DPI 屏上界面会被系统拉伸成模糊的。"""
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)    # type: ignore[attr-defined]
        except Exception:                                      # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()          # type: ignore[attr-defined]
    except Exception:                                          # noqa: BLE001
        pass


def main() -> int:
    """启动界面。`--gtk` 时改用 GTK3 那套（只在 Linux 上有意义）。"""
    from .cli import bootstrap
    bootstrap(verbose=True)               # 搬老配置 + 定下方言（判风险/提示词都依赖它）

    if "--gtk" in sys.argv:
        try:
            from .gui_gtk3 import main as gtk_main
            return gtk_main()
        except ImportError as exc:
            print("GTK3 界面起不来（多半是没装 PyGObject）：%s" % exc)
            print("  改用自带界面继续启动。")

    _enable_dpi_awareness()
    try:
        app = NlcApp()
    except tk.TclError as exc:
        print("启动不了图形界面：%s" % exc)
        print("  没有图形环境的话，请用命令行：nlc tr / lookup / explain / risk")
        return 2
    app.root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
