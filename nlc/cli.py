# -*- coding: utf-8 -*-
"""NLC 命令行界面（CLI）。

设计原则：**核心能力必须能纯命令行用**，GUI 只是锦上添花（见规则详解第七节）。
因此这个 CLI 不是附属品，它是 NLC 的第一公民。

用法（Windows 上把 `python3` 换成 `python`）：
    python -m nlc tokenize "du -sh *"       切词块
    python -m nlc risk "rm -rf /"           判风险
    python -m nlc explain "dir /b"          解释一条现成命令
    python -m nlc providers                 列出所有 AI 通道
    python -m nlc set-key deepseek sk-xxx   填密钥
    python -m nlc test deepseek             测试通道
    python -m nlc shell                     看/改「判哪种命令行」
    python -m nlc doctor                    一条命令做体检（出问题时先跑这个）
    python -m nlc danger status             查看完全访问模式
"""

from __future__ import annotations

import argparse
import os
import platform as _platform_stdlib          # ★ 标准库的 platform，与本项目的 nlc.platform 区分开
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .platform import (IS_WINDOWS, OS_LABEL, SHELL_BASH, SHELL_CMD,
                       SHELL_POWERSHELL, SHELLS, active_shell, default_shell,
                       launcher_dir, python_cmd, set_shell, shell_hint, shell_label)
from .providers import (CONFIG_PATH, OFFLINE, PRESETS, SECRETS_PATH, ChannelError,
                        fallback_chain, get_api_key, get_provider, load_config,
                        save_config, secure_secrets_report, set_api_key, test)
from .risk import ALLOW, BLOCK, CONFIRM, assess, explain
from .tokenizer import shell_words, summarize, tokenize

_LEVEL_MARK = {ALLOW: "[绿] 可执行", CONFIRM: "[红] 需二次确认", BLOCK: "[灰] 已禁止"}


def _py() -> str:
    """当前平台该把 Python 写成什么（Windows=python，Linux=python3）。"""
    return python_cmd()


def bootstrap(verbose: bool = False) -> str:
    """所有入口（CLI / GUI / MCP）启动时都要先跑一遍的准备工作。

    做两件事：
        1. 老配置搬家 —— 从 Linux 版的位置（~/.config/nlc）搬到 Windows 版的位置
           （%APPDATA%\\nlc）。只复制不删除，且新位置已有配置时什么都不做。
        2. 按配置设好「判哪种命令行」（cmd / PowerShell / bash）——
           判风险与写提示词都依赖它。

    为什么集中在这里：三处入口逻辑一致，散着写迟早会漏一处。
    """
    from .providers import migrate_legacy_config

    if verbose:
        for line in migrate_legacy_config():
            print("  " + line)

    try:
        cfg = load_config()
    except Exception:                                    # noqa: BLE001 - 配置坏了不该拦住 CLI
        return set_shell(None)
    return set_shell((cfg.get("ui") or {}).get("shell") or None)


#: 兼容旧名字
bootstrap_shell = bootstrap


def _cmd_tokenize(args: argparse.Namespace) -> int:
    toks = tokenize(args.command)
    if not toks:
        print("（没有切出任何词块）")
        return 0
    print("%s" % summarize(toks))
    print()
    print("  下标  类型     空白  词块")
    print("  ----  -------  ----  ----")
    for t in toks:
        print("  %-4d  %-7s  %-4s  %s" % (
            t.index, "单词" if t.kind == "word" else "符号",
            "有" if t.gap_before else "粘连", t.text))
    print()
    print("  壳词分组（风险引擎用的视图）：%s" % shell_words(toks))
    return 0


def _cmd_risk(args: argparse.Namespace) -> int:
    a = assess(args.command)
    print(_LEVEL_MARK.get(a.level, a.level))
    print("  %s" % a.reason)
    print("  命中规则：%s %s" % (a.rule_id or "-", a.category or "-"))
    print("  可执行入口：%s" % ("有" if a.executable else "无（点不动）"))
    print("  执行时弹危险框：%s" % ("是" if a.needs_dialog else "否"))
    return 0


def _provider_or_die(cfg):
    """挑一个能用的通道（按降级链，第一个填了密钥的就用）。"""
    from .providers import test as chan_test
    for pid in fallback_chain(cfg):
        if pid == OFFLINE:
            continue
        p = get_provider(cfg, pid)
        if not p:
            continue
        if p.needs_key and not get_api_key(p.id, p.key_env):
            continue
        ok, _ = chan_test(p)
        if ok:
            return p
    return None


def _show(tr, killed=()) -> None:
    """把一次翻译结果渲染成人能看的样式。"""
    from .translator import render_blocks
    print("  命令   %s" % render_blocks(tr, killed))
    print("  词块   %d 个" % len(tr.blocks))
    if tr.summary:
        print("  说明   %s" % tr.summary)
    if tr.risk:
        print("  风险   %s —— %s" % (_LEVEL_MARK.get(tr.risk.level, tr.risk.level), tr.risk.reason))
        print("         命中规则 %s %s" % (tr.risk.rule_id, tr.risk.category))
        print("         可执行入口：%s ｜ 要弹危险框：%s"
              % ("有" if tr.risk.executable else "无（点不动）",
                 "是" if tr.risk.needs_dialog else "否"))
    if tr.notes:
        print("  注意   %s" % tr.notes)
    if tr.blocks:
        print("  逐词块解释：")
        for b in tr.blocks:
            glue = "" if b.gap_before else "（粘连）"
            print("    [%2d] %-10s %-4s%-6s %s" % (
                b.index, b.text, "单词" if b.kind == "word" else "符号", glue, b.meaning))
    print("  通道   %s ｜ 角色卡 %s" % (tr.source or "-", _active_profile(load_config()).id))


def _cmd_lookup(args: argparse.Namespace) -> int:
    from .translator import TranslatorError, lookup
    cfg = load_config()
    p = _provider_or_die(cfg)
    if not p:
        print("没有可用的 AI 通道。先填密钥：%s -m nlc set-key deepseek sk-xxx" % _py())
        return 2
    try:
        tr = lookup(args.term, p, profile=_active_profile(cfg))
    except TranslatorError as exc:
        print("查词失败：%s" % exc)
        return 1
    print("查的词：%s\n" % args.term)
    print("  结果   %s" % tr.command)
    print("  说明   %s" % tr.summary)
    print("  解释   %s" % (tr.blocks[0].meaning if tr.blocks else "-"))
    if tr.notes:
        print("  补充   %s" % tr.notes)
    if tr.risk:
        print("  风险   %s" % _LEVEL_MARK.get(tr.risk.level, tr.risk.level))
    return 0


def _cmd_tr(args: argparse.Namespace) -> int:
    """命令行翻译：一句话 → 命令。可用 --kill 模拟点掉词块后再生成。"""
    from .translator import TranslatorError, regenerate, translate
    cfg = load_config()
    p = _provider_or_die(cfg)
    if not p:
        print("没有可用的 AI 通道。先填密钥：%s -m nlc set-key deepseek sk-xxx" % _py())
        return 2
    try:
        if args.kill:
            base = translate(args.sentence, p, profile=_active_profile(cfg))
            print("  原始   %s\n" % base.command)
            killed = [int(x) for x in args.kill.split(",")]
            tr = regenerate(args.sentence, base, killed, p, profile=_active_profile(cfg))
            print("  点掉   %s\n" % [base.blocks[i].text for i in killed if i < len(base.blocks)])
        else:
            tr = translate(args.sentence, p, profile=_active_profile(cfg))
    except (TranslatorError, ValueError) as exc:
        print("翻译失败：%s" % exc)
        return 1
    print("需求：%s\n" % args.sentence)
    _show(tr)
    return 0


def _cmd_compose(args: argparse.Namespace) -> int:
    from .translator import TranslatorError, compose
    cfg = load_config()
    p = _provider_or_die(cfg)
    if not p:
        print("没有可用的 AI 通道。先填密钥：%s -m nlc set-key deepseek sk-xxx" % _py())
        return 2
    try:
        tr = compose(args.commands, p, profile=_active_profile(cfg))
    except TranslatorError as exc:
        print("组合失败：%s" % exc)
        return 1
    print("组合后的脚本：")
    for line in tr.command.splitlines():
        print("    " + line)
    print()
    if tr.summary:
        print("  说明   %s" % tr.summary)
    if tr.risk:
        print("  风险   %s —— %s" % (_LEVEL_MARK.get(tr.risk.level, tr.risk.level), tr.risk.reason))
    if tr.notes:
        print("  注意   %s" % tr.notes)
    return 0


def _cmd_exp(args: argparse.Namespace) -> int:
    """解释一条现成命令。默认调大模型；--offline 则只用本地风险引擎。"""
    from .translator import TranslatorError, explain as tr_explain
    a = assess(args.command)
    print("本地风险判定：%s" % _LEVEL_MARK.get(a.level, a.level))
    print("  %s\n" % a.reason)
    if args.offline:
        return 0
    cfg = load_config()
    p = _provider_or_die(cfg)
    if not p:
        print("（没有可用的 AI 通道，只给出上面的本地判定。填密钥后可获得逐词块解释。）")
        return 0
    try:
        tr = tr_explain(args.command, p, profile=_active_profile(cfg))
    except TranslatorError as exc:
        print("解释失败：%s" % exc)
        return 1
    print("逐词块解释：")
    for b in tr.blocks:
        glue = "" if b.gap_before else "（粘连）"
        print("  [%2d] %-10s %-4s%-6s %s" % (
            b.index, b.text, "单词" if b.kind == "word" else "符号", glue, b.meaning))
    return 0


def _active_profile(cfg):
    """取当前生效的角色卡（不存在就退回默认）。"""
    from .prompts import get_profile
    pid = (cfg.get("ui") or {}).get("prompt_profile") or "concise"
    try:
        return get_profile(pid)
    except Exception:
        return get_profile("concise")


def _cmd_prompt(args: argparse.Namespace) -> int:
    """提示词角色卡：查看 / 切换 / 看完整提示词。"""
    from .prompts import PROFILES_PATH, build_system_prompt, load_profiles, save_profiles
    cfg = load_config()
    profs = load_profiles()
    cur = (cfg.get("ui") or {}).get("prompt_profile") or "concise"

    if args.action == "list":
        print("提示词角色卡（当前生效：%s）\n" % cur)
        for pid, p in profs.items():
            mark = "→" if pid == cur else " "
            print(" %s %-10s %-2s %-8s %-4s 温度=%.1f  上限=%d字  %s"
                  % (mark, pid, p.emoji, p.name, "内置" if p.builtin else "自定义",
                     p.temperature, p.max_tokens, p.desc))
        print("\n配置文件：%s" % PROFILES_PATH)
        return 0

    if args.action == "use":
        if args.id not in profs:
            print("没有这张卡：%s（现有：%s）" % (args.id, "、".join(profs)))
            return 1
        cfg.setdefault("ui", {})["prompt_profile"] = args.id
        save_config(cfg)
        print("已切换到角色卡：%s %s（%s）" % (profs[args.id].emoji, profs[args.id].name, args.id))
        return 0

    if args.action == "show":
        pid = args.id or cur
        if pid not in profs:
            print("没有这张卡：%s" % pid)
            return 1
        task = args.task
        print("═══ 角色卡 %s（%s）· 任务 %s 的完整系统提示词 ═══\n" % (pid, profs[pid].name, task))
        print(build_system_prompt(profs[pid], task))
        return 0

    if args.action == "new":
        # 复制一张内置卡作为自定义卡的起点
        base = profs.get(args.base or cur)
        if base is None:
            print("基准卡不存在")
            return 1
        d = base.to_dict()
        d["id"] = args.id
        d["name"] = args.name or args.id
        d["desc"] = "自定义卡（复制自 %s）" % base.id
        d["builtin"] = False
        profs[args.id] = type(base).from_dict(d)
        save_profiles(profs)
        cfg.setdefault("ui", {})["prompt_profile"] = args.id
        save_config(cfg)
        print("已新建自定义卡「%s」并切换过去。改它：编辑 %s" % (args.id, PROFILES_PATH))
        print("（以后的会话里可以专门训练这套提示词）")
        return 0

    print("未知操作")
    return 1


def _cmd_bl(args: argparse.Namespace) -> int:
    """黑名单：查看两个区 / 永久拉黑 / 解除。"""
    from .blacklist import BLACKLIST_PATH, Blacklist
    bl = Blacklist()

    if args.action == "show":
        print("黑名单文件：%s\n" % BLACKLIST_PATH)
        print("【临时区】（下标，只在本次查询有效）共 %d 条" % len(bl.session))
        for k in bl.session:
            print("   [%2d] %-12s %s" % (k.index, k.text, k.reason or ""))
        if not bl.session:
            print("   （空）")
        print("\n【永久区】（词块文本，跨会话生效）共 %d 条" % len(bl.permanent))
        for e in bl.permanent.values():
            print("   %-12s %-12s %s" % (e.text, e.added, e.reason or ""))
        if not bl.permanent:
            print("   （空）")
        return 0

    if args.action == "ban":
        for t in args.texts:
            bl.ban(t, args.reason or "")
        print("已永久拉黑：%s" % "、".join(args.texts))
        return 0

    if args.action == "unban":
        n = 0
        for t in args.texts:
            n += 1 if bl.unban(t) else 0
        print("已解除 %d 条" % n)
        return 0

    if args.action == "clear":
        bl.permanent.clear()
        bl.save()
        print("永久区已清空")
        return 0

    print("未知操作")
    return 1


def _cmd_wb(args: argparse.Namespace) -> int:
    """个人词库：增删查 / 分享码 / 导入导出。"""
    from .wordbank import WORDBANK_PATH, WordBank, WordBankError
    wb = WordBank()

    if args.action == "list":
        cfg = load_config()
        on = (cfg.get("ui") or {}).get("translate_enabled", True)
        print("词库文件：%s" % WORDBANK_PATH)
        print("翻译功能：%s" % ("开着（词库命中会额外标注）" if on else "关着（回车直接跑词库）"))
        print("共 %d 条\n" % len(wb))
        if not wb:
            print("  （空）")
        for e in wb.all():
            flag = "" if e.verified else "  ⚠️ 未经审核"
            print("  %-14s → %-14s %s%s" % (e.term, e.token, e.note, flag))
        return 0

    if args.action == "add":
        try:
            e = wb.add(args.term, args.token, note=args.note or "")
        except WordBankError as exc:
            print("加不了：%s" % exc)
            return 1
        hint = "" if WordBank.looks_like_token(e.token) else \
            "（提示：`%s` 看起来不像「一个英文单词或一个符号」，但按规则仍已保存）" % e.token
        print("已添加：%s → %s %s" % (e.term, e.token, hint))
        return 0

    if args.action == "rm":
        print("已删除" if wb.remove(args.term) else "词库里没有「%s」" % args.term)
        return 0

    if args.action == "get":
        e = wb.lookup(args.term)
        if not e:
            print("词库里没有「%s」" % args.term)
            return 1
        print("%s → %s%s" % (e.term, e.token, ("   # " + e.note) if e.note else ""))
        print("（纯查表命中，不做语义理解）")
        return 0

    if args.action == "code":
        print(wb.export_code())
        return 0

    if args.action == "export":
        p = wb.export_file(args.path)
        print("已导出到 %s" % p)
        return 0

    if args.action == "import":
        try:
            if args.src and os.path.exists(args.src):
                ok, skipped = wb.import_file(args.src, overwrite=args.overwrite)
            else:
                ok, skipped = wb.import_code(args.src, overwrite=args.overwrite)
        except WordBankError as exc:
            print("导入失败：%s" % exc)
            return 1
        print("导入成功 %d 条（全部标记为「未经审核」，第一次执行会强制确认）" % ok)
        for s in skipped[:10]:
            print("  跳过：%s" % s)
        return 0

    print("未知操作")
    return 1


def _cmd_translate_switch(args: argparse.Namespace) -> int:
    """个人词库的「翻译开关」。"""
    cfg = load_config()
    ui = cfg.setdefault("ui", {})
    if args.action == "status":
        on = ui.get("translate_enabled", True)
        print("翻译功能：%s" % ("开着" if on else "关着"))
        print("  " + ("走 AI 翻译；词库命中的结果会额外标注「来自个人词库」" if on
                      else "只查词库，不调 AI。输入框变快捷方式，回车直接跑对应命令"))
        print("  ⚠️ 但危险命令**仍然要过风险判定** —— 这道闸门不因关掉翻译而放开。")
        return 0
    ui["translate_enabled"] = (args.action == "on")
    save_config(cfg)
    print("翻译功能已%s" % ("开启" if ui["translate_enabled"] else "关闭"))
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    """启动 MCP 服务端（stdio 传输）。延迟导入，不影响其它命令。"""
    from .mcp_server import main as mcp_main
    argv = []
    if args.selftest:
        argv.append("--selftest")
    if args.list_tools:
        argv.append("--list")
    return mcp_main(argv)


def _cmd_gui(args: argparse.Namespace) -> int:
    """启动图形界面。延迟导入：没有图形环境时不会拖累其它命令。

    ★ 平台差异：
        - Windows：用 Python 自带的 tkinter，**不需要装任何东西**
        - Linux  ：用 GTK3（用户环境缺 GTK3 时只影响界面，CLI/MCP 照常）
    """
    try:
        from .gui import main as gui_main
    except ImportError as exc:
        print("启动不了图形界面：%s" % exc)
        if IS_WINDOWS:
            print("  Windows 上用 tkinter，它是 Python 安装包里就带的 —— "
                  "若报错说明这套 Python 是被精简过的（缺 tkinter）。")
            print("  纯命令行环境请用 nlc tr / lookup / explain / risk。")
        else:
            print("  GTK3 只在有图形环境的机器上有；纯命令行环境请用 nlc tr / lookup / explain。")
        return 2
    return gui_main()


def _cmd_shell(args: argparse.Namespace) -> int:
    """查看 / 切换「判哪种命令行」。

    ★ 这一条是 Windows 版新增的，而且是刚需：
      同一个需求在 cmd 与 PowerShell 里的写法完全不同（`dir` vs `Get-ChildItem`），
      判风险用的转义符与开关也不一样。不告诉 NLC 用哪种，它就只能猜。
    """
    cfg = load_config()
    ui = cfg.setdefault("ui", {})

    if args.action == "status" or args.action is None:
        cur = active_shell()
        print("当前判定的命令行：%s（%s）" % (shell_label(cur), cur))
        print("  平台：%s" % OS_LABEL)
        print("  平台默认：%s" % shell_label(default_shell()))
        print("  配置里写的：%s" % (ui.get("shell") or "（空 = 跟随平台默认）"))
        print()
        print("  可选项：")
        for k in SHELLS:
            mark = "→" if k == cur else " "
            print("   %s %-12s %s" % (mark, k, shell_label(k)))
        print()
        print("  切换：%s -m nlc shell set cmd" % _py())
        return 0

    if args.action == "set":
        want = (args.kind or "").strip().lower()
        if want == "ps":                      # 顺手支持一个简写
            want = SHELL_POWERSHELL
        if want in ("", "auto", "default"):
            ui["shell"] = ""
            save_config(cfg)
            print("已改为跟随平台默认：%s（%s）" % (shell_label(set_shell(None)), active_shell()))
            return 0
        if want not in SHELLS:
            print("不认识这种命令行：%s（可选：%s、auto）" % (want, "、".join(SHELLS)))
            return 1
        ui["shell"] = want
        save_config(cfg)
        set_shell(want)
        print("已切换为：%s（%s）" % (shell_label(want), want))
        return 0

    print("未知操作")
    return 1


def _cmd_providers(args: argparse.Namespace) -> int:
    cfg = load_config()
    active = cfg.get("active", "auto")
    print("运行平台：%s" % OS_LABEL)
    print("判定的命令行：%s（%s）" % (shell_label(), active_shell()))
    print("配置文件：%s" % CONFIG_PATH)
    if IS_WINDOWS:
        print("密钥文件：%s（已尝试用 icacls 收紧到只有本人可访问）" % SECRETS_PATH)
    else:
        print("密钥文件：%s（权限 600，只有本人可读写）" % SECRETS_PATH)
    print("当前策略：%s" % ("自动（按下面的顺序依次尝试）" if active == "auto" else "固定用 %s" % active))
    print()
    print("  %-12s %-18s %-9s %-9s %s" % ("ID", "名称", "类型", "密钥", "地址/模型"))
    print("  " + "-" * 88)
    for pid in fallback_chain(cfg):
        if pid == OFFLINE:
            print("  %-12s %-18s %-9s %-9s %s" % (pid, "本地知识库兜底", "offline", "不需要", "永不失败"))
            continue
        p = get_provider(cfg, pid)
        if not p:
            continue
        key = get_api_key(p.id, p.key_env)
        print("  %-12s %-18s %-9s %-9s %s" % (
            p.id, p.name, p.kind,
            ("已填" if key else ("需要" if p.needs_key else "不需要")),
            (p.base_url or "(未填)") + ("  " + p.model if p.model else "")))
    print()
    print("  可用的预置通道：%s" % "、".join(PRESETS.keys()))
    return 0


def _cmd_set_key(args: argparse.Namespace) -> int:
    path = set_api_key(args.provider, args.key.strip())
    if args.key.strip():
        print("已保存 %s 的 API Key 到 %s" % (args.provider, path))
    else:
        print("已清除 %s 的 API Key" % args.provider)
    # ★ 如实报告密钥文件到底被保护到什么程度 —— 不假装成功
    for line in secure_secrets_report():
        print("  权限：%s" % line)
    if IS_WINDOWS:
        print("  说明：Windows 上 chmod 改不了 NTFS 权限，所以走的是 icacls。"
              "若上面显示失败，说明这台机器的权限模型不配合（例如盘是 FAT32）。")
    return 0


def _cmd_test(args: argparse.Namespace) -> int:
    cfg = load_config()
    targets = [args.provider] if args.provider else fallback_chain(cfg)
    worst = 0
    for pid in targets:
        if pid == OFFLINE:
            print("  %-12s  %s" % (pid, "离线兜底：永远可用"))
            continue
        p = get_provider(cfg, pid)
        if not p:
            print("  %-12s  没有这个通道" % pid)
            worst = 1
            continue
        if not p.enabled and not args.all:
            print("  %-12s  已停用（加 --all 也测）" % pid)
            continue
        ok, msg = test(p)
        print("  %-12s  %s  %s" % (pid, "通过" if ok else "不通", msg))
        if ok:
            break          # 降级链上第一个能用的就够了
    return worst


def _cmd_danger(args: argparse.Namespace) -> int:
    cfg = load_config()
    cur = bool(cfg.get("ui", {}).get("danger_full_access", False))
    if args.action == "status":
        print("完全访问模式：%s" % ("★ 已开启 ★" if cur else "关闭（默认）"))
        if cur:
            print("  危险提示已跳过。但「禁止」档仍然点不动 —— 这一档没有开关。")
        return 0
    if args.action == "on":
        if args.confirm != "我明白风险":
            print("拒绝开启。")
            print("  完全访问模式会跳过所有危险确认，请用下面这条命令、并原样输入确认短语：")
            print('      %s -m nlc danger on --confirm "我明白风险"' % _py())
            return 2
        cfg.setdefault("ui", {})["danger_full_access"] = True
        save_config(cfg)
        print("★ 完全访问模式已开启。危险提示不再弹出。")
        print("  关闭：%s -m nlc danger off" % _py())
        return 0
    cfg.setdefault("ui", {})["danger_full_access"] = False
    save_config(cfg)
    print("完全访问模式已关闭，危险提示恢复。")
    return 0


def _cmd_version(_: argparse.Namespace) -> int:
    print("NLC（自然语言命令行）v%s" % __version__)
    print("Natural Language Command-line")
    return 0


# --------------------------------------------------------------------------- #
# doctor —— 一条命令做体检
# --------------------------------------------------------------------------- #
#: 体检要跑的四条自测。key 是文件名，label 是给人看的名字。
#: ⚠️ 刻意不 import 这些测试文件 —— 它们是脚本（顶层就自己跑），
#:    这里只把它们当子进程跑，拿返回码和最后一行摘要。
_SELF_TESTS = [
    ("selftest.py", "Linux 侧基线"),
    ("selftest_win.py", "Windows 侧"),
    ("mcp_stdio_test.py", "MCP 端到端"),
    ("gui_smoke.py", "界面冒烟"),
]

#: 体检结果分三档：ok / warn / bad。只有 bad 才让退出码非零。
_OK, _WARN, _BAD = "ok", "warn", "bad"
_MARK = {_OK: "[ 正常 ]", _WARN: "[ 注意 ]", _BAD: "[ 问题 ]"}


def _w(s: str) -> int:
    """字符串在终端里占几列。

    ★ 为什么不用 len()：中文一个字占两列。用 len() 对齐会歪。
      这就是《三个反复踩的坑》里"先想清楚哪个视图对得上"的同一个思路 ——
      宽度也要按渲染结果算，不能按字符数算。
    """
    import unicodedata

    n = 0
    for ch in s:
        n += 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
    return n


def _pad(s: str, width: int) -> str:
    """按显示宽度左对齐补空格。

    ⚠️ 已经超宽就**原样返回、不截断也不补**：
       截断会把路径/通道名砍掉一半，比不齐难看得多；补空格只会让列更歪。
       列与列之间的间隔由调用方格式串里的空格负责，这里只管"补到齐"。
    """
    gap = width - _w(s)
    return s + " " * gap if gap > 0 else s


def _run_selftest(name: str) -> tuple:
    """跑一条自测，返回 (档位, 说明)。抓不到解释器就如实说，不假装通过。"""
    import subprocess

    path = Path(__file__).resolve().parent.parent / "tests" / name
    if not path.exists():
        return _WARN, "测试文件不在（%s），装到精简版或缺 tests 目录时会这样" % name
    try:
        # ★ 拿字节、不解码：子进程输出走 Windows 的 OEM 代码页（中文系统是 GBK），
        #   在 UTF-8 模式下解码会抛 UnicodeDecodeError（见《三个反复踩的坑》坑二）。
        p = subprocess.run([sys.executable, str(path)], capture_output=True,
                           cwd=str(path.parent.parent), timeout=600)
    except Exception as exc:                                    # noqa: BLE001
        return _BAD, "跑不起来：%s" % exc
    out = (p.stdout or b"").decode("utf-8", errors="replace")
    summary = ""
    for line in reversed(out.splitlines()):
        if "通过" in line and ("失败" in line or "项" in line):
            summary = line.strip()
            break
    if p.returncode != 0:
        return _BAD, summary or "返回码 %d（没抓到摘要，可能中途崩了）" % p.returncode
    return _OK, summary or "通过"


def _doctor_line(grade: str, label: str, value: str) -> None:
    print("  %s  %s  %s" % (_MARK[grade], _pad(label, 12), value))


def _cmd_doctor(args: argparse.Namespace) -> int:
    """一条命令做体检：平台 / Python / 界面能力 / 路径 / 方言 / 密钥 / 四条自测。

    为什么要有它：排查一次问题本来要跑 providers、shell、test 好几条，
    而且每条报的东西都不一样。体检就是**一次报全**，并且明确指出
    「哪一条是真正的病根、哪一条只是提醒」。
    """
    from . import platform as plat
    from .providers import CONFIG_PATH, SECRETS_PATH, migrate_legacy_config

    cfg = load_config()
    worst = _OK

    def note(grade: str) -> None:
        nonlocal worst
        if grade == _BAD or (grade == _WARN and worst == _OK):
            worst = grade

    print("═" * 64)
    print("  NLC v%s 体检" % __version__)
    print("═" * 64)

    # ── 一、运行环境 ──────────────────────────────
    print("\n【一】运行环境")
    _doctor_line(_OK, "平台", OS_LABEL)
    _doctor_line(_OK, "Python", "%s  (%s)" % (_platform_stdlib.python_version(), plat.python_exe()))
    _doctor_line(_OK, "权限", "管理员" if plat.is_elevated() else "普通用户（够用，不需要管理员）")

    # tkinter：这是最容易踩的一条 —— 托管/精简版 Python 不带 tkinter，
    # 表现是「跑命令行一切正常，一开界面就 ModuleNotFoundError」。
    try:
        import tkinter

        tk_ver = "有（Tk %s）" % getattr(tkinter, "TkVersion", "?")
    except Exception as exc:                                    # noqa: BLE001
        tk_ver = "没有（%s）" % exc
    grade_tk = _OK if tk_ver.startswith("有") else _BAD
    _doctor_line(grade_tk, "图形界面", tk_ver)
    note(grade_tk)
    if grade_tk == _BAD:
        print("             → 界面在这个 Python 上起不来。换一个带 tkinter 的 Python，")
        print("               纯命令行的功能不受影响。")

    # ── 二、装在哪 / 配置在哪 ─────────────────────
    print("\n【二】位置")
    dev_root = Path(__file__).resolve().parent.parent
    inst_root = plat.launcher_dir()
    installed = (inst_root / ".nlc-install-manifest").exists()
    _doctor_line(_OK, "正在运行", str(dev_root))
    if installed:
        _doctor_line(_OK, "已安装到", str(inst_root))
    else:
        _doctor_line(_WARN, "已安装到", "没装（用 %s install.cmd 装，或直接双击 nlc.cmd 用免安装版）" % _py())
        note(_WARN)
    _doctor_line(_OK if CONFIG_PATH.exists() else _WARN, "配置文件", str(CONFIG_PATH))
    _doctor_line(_OK if SECRETS_PATH.exists() else _WARN, "密钥文件", str(SECRETS_PATH))
    if not CONFIG_PATH.exists():
        note(_WARN)
    legacy = [ln for ln in migrate_legacy_config() if "搬" in ln or "复制" in ln]
    if legacy:
        for ln in legacy:
            print("             · %s" % ln)

    # ── 三、判哪种命令行 ──────────────────────────
    print("\n【三】判哪种命令行")
    cur = active_shell()
    ui_shell = (cfg.get("ui") or {}).get("shell") or ""
    same_as_default = (cur == default_shell())
    _doctor_line(_OK, "当前", "%s（%s）" % (shell_label(cur), cur))
    _doctor_line(_OK, "平台默认", "%s（%s）%s" % (
        shell_label(default_shell()), default_shell(),
        "" if same_as_default else "  ← 当前不是默认"))
    _doctor_line(_OK, "配置里写的", ui_shell or "（空 = 跟随平台默认）")
    if IS_WINDOWS and cur == SHELL_CMD:
        print("             · 提示：PowerShell 用户记得 %s -m nlc shell set ps" % _py())

    # ── 四、AI 通道与密钥 ─────────────────────────
    print("\n【四】AI 通道（不填密钥也能切词块、判风险、查词库）")
    #: 真正会被用的那一个（降级链上第一个可用的）—— 只有它缺密钥才是真问题
    chain = [pid for pid in fallback_chain(cfg) if pid != OFFLINE]
    active_pid = next((pid for pid in chain
                       if (p := get_provider(cfg, pid)) and p.enabled
                       and (not p.needs_key or get_api_key(p.id, p.key_env))), "")
    print("  %s  %s  %s  %s" % (" " * 8, _pad("ID", 14), _pad("密钥", 10), "备注"))
    for pid in fallback_chain(cfg):
        if pid == OFFLINE:
            print("  %s  %s  %s  %s" % (_MARK[_OK], _pad(pid, 14), _pad("不需要", 10), "永远可用的兜底"))
            continue
        p = get_provider(cfg, pid)
        if not p:
            continue
        key = get_api_key(p.id, p.key_env)
        if key or not p.needs_key:
            g = _OK
            note_txt = "已填密钥" if key else "免密钥"
        elif not p.enabled:
            g = _OK
            note_txt = "已停用（不参与降级链）"
        else:
            # ★ 只有"启用中又缺密钥、且降级链上没别人能顶上的"才算问题。
            #   链上后面还有可用通道时，这里报问题就是在制造焦虑。
            g = _WARN if (not active_pid and pid == (chain[0] if chain else "")) else _OK
            note_txt = "缺密钥" + ("，翻译会退回本地" if active_pid else "")
        print("  %s  %s  %s  %s" % (_MARK[g], _pad(p.id, 14),
                                    _pad("已填" if key else "—", 10), note_txt))
    if active_pid:
        print("             → 翻译实际会用：%s" % active_pid)
    else:
        note(_WARN)
        print("             → 翻译用不了（降级链上没有一个通道就绪）。填一个：")
        print("               %s -m nlc set-key deepseek sk-…" % _py())
        print("               切词块 / 判风险 / 查词库 / MCP 判风险 都不需要密钥，照常可用。")

    # ── 五、四条自测 ──────────────────────────────
    print("\n【五】自测")
    if not args.no_tests:
        for name, label in _SELF_TESTS:
            g, msg = _run_selftest(name)
            _doctor_line(g, label, msg)
            note(g)
    else:
        _doctor_line(_WARN, "自测", "按 --no-tests 跳过了")

    # ── 结论 ─────────────────────────────────────
    print()
    if worst == _OK:
        print("  结论：一切正常。")
        return 0
    if worst == _WARN:
        print("  结论：能用，但有上面标「注意」的项。不影响切词块与判风险。")
        return 0
    print("  结论：上面标「问题」的项需要处理。先看《07_已知坑与禁忌》里的排查顺序。")
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nlc", description="NLC —— 在人与命令行之间加一层 AI 翻译。AI 只翻译，不执行。")
    p.add_argument("--version", action="store_true", help="显示版本")
    sub = p.add_subparsers(dest="cmd")

    t = sub.add_parser("tokenize", help="把命令切成命令词块")
    t.add_argument("command")
    t.set_defaults(func=_cmd_tokenize)

    r = sub.add_parser("risk", help="判定一条命令的风险档位")
    r.add_argument("command")
    r.set_defaults(func=_cmd_risk)

    e = sub.add_parser("explain", help="解释一条命令（本地判风险 + 大模型逐词块解释）")
    e.add_argument("command")
    e.add_argument("--offline", action="store_true", help="只用本地风险引擎，不调大模型")
    e.set_defaults(func=_cmd_exp)

    lk = sub.add_parser("lookup", help="① 查词：一个词 → 一个命令词块 + 词义解释")
    lk.add_argument("term")
    lk.set_defaults(func=_cmd_lookup)

    tt = sub.add_parser("tr", help="② 命令行：一句话 → 命令 + 逐词块解释")
    tt.add_argument("sentence")
    tt.add_argument("--kill", default="", help="点掉这些下标的词块后重新生成，如 8,9,10")
    tt.set_defaults(func=_cmd_tr)

    cp = sub.add_parser("compose", help="③ 对话式脚本：把若干条命令拼成一个脚本")
    cp.add_argument("commands", nargs="+")
    cp.set_defaults(func=_cmd_compose)

    sub.add_parser("providers", help="列出所有 AI 通道").set_defaults(func=_cmd_providers)

    sub.add_parser("gui", help="启动图形界面（Windows 用 tkinter，Linux 用 GTK3）") \
       .set_defaults(func=_cmd_gui)

    sh = sub.add_parser("shell", help="判哪种命令行：cmd / powershell / bash（Windows 版新增）")
    sh.add_argument("action", nargs="?", default="status",
                    choices=["status", "set"])
    sh.add_argument("kind", nargs="?", default="")
    sh.set_defaults(func=_cmd_shell)

    mc = sub.add_parser("mcp", help="启动 MCP 服务端（供 Claude/Codex/Cursor 等调用）")
    mc.add_argument("--selftest", action="store_true", help="不接客户端，自己跑一遍工具自检")
    mc.add_argument("--list-tools", action="store_true", help="列出暴露的工具")
    mc.set_defaults(func=_cmd_mcp)

    # ── 提示词角色卡 ────────────────────────────────
    pr = sub.add_parser("prompt", help="提示词角色卡（查看 / 切换 / 看完整提示词）")
    pr.add_argument("action", choices=["list", "use", "show", "new"])
    pr.add_argument("id", nargs="?", default="")
    pr.add_argument("--task", default="translate",
                    choices=["translate", "lookup", "explain", "compose"])
    pr.add_argument("--base", default="", help="new 时以哪张卡为起点")
    pr.add_argument("--name", default="", help="new 时给新卡起的名字")
    pr.set_defaults(func=_cmd_prompt)

    # ── 黑名单 ────────────────────────────────────
    bl = sub.add_parser("bl", help="命令词块黑名单（临时区 / 永久区）")
    bl.add_argument("action", choices=["show", "ban", "unban", "clear"])
    bl.add_argument("texts", nargs="*", default=[])
    bl.add_argument("--reason", default="")
    bl.set_defaults(func=_cmd_bl)

    # ── 个人词库 ──────────────────────────────────
    wb = sub.add_parser("wb", help="个人定制词库")
    wb.add_argument("action", choices=["list", "add", "rm", "get", "code", "export", "import"])
    wb.add_argument("term", nargs="?", default="")
    wb.add_argument("token", nargs="?", default="")
    wb.add_argument("--note", default="")
    wb.add_argument("--path", default="", help="export 的目标文件")
    wb.add_argument("--src", default="", help="import 的来源：分享码或文件路径")
    wb.add_argument("--overwrite", action="store_true")
    wb.set_defaults(func=_cmd_wb)

    # ── 翻译开关 ──────────────────────────────────
    ts = sub.add_parser("translate-switch", help="个人词库的翻译开关（开/关/状态）")
    ts.add_argument("action", choices=["on", "off", "status"])
    ts.set_defaults(func=_cmd_translate_switch)

    k = sub.add_parser("set-key", help="填 API Key（留空则清除）")
    k.add_argument("provider")
    k.add_argument("key", nargs="?", default="")
    k.set_defaults(func=_cmd_set_key)

    s = sub.add_parser("test", help="测试 AI 通道是否可用")
    s.add_argument("provider", nargs="?")
    s.add_argument("--all", action="store_true")
    s.set_defaults(func=_cmd_test)

    d = sub.add_parser("danger", help="完全访问模式（跳过危险提示）")
    d.add_argument("action", choices=["status", "on", "off"])
    d.add_argument("--confirm", default="")
    d.set_defaults(func=_cmd_danger)

    doc = sub.add_parser("doctor", help="一条命令做体检：平台 / 界面能力 / 路径 / 方言 / 密钥 / 四条自测")
    doc.add_argument("--no-tests", action="store_true", help="跳过四条自测（只想看环境时更快）")
    doc.set_defaults(func=_cmd_doctor)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    # ★ 先做启动准备：搬老配置 + 定下方言。判风险与提示词都依赖后者。
    bootstrap(verbose=True)

    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False):
        return _cmd_version(args)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
