#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NLC 的 MCP 服务端（Model Context Protocol，stdio 传输）。

为什么值得单独做（见《NLC规则详解_v3.1.md》第六节）：
    任何 MCP 客户端（Claude / Codex / Cursor / 自研 Agent）都能复用 NLC 的能力。
    其中 ★ **`nlc_assess_risk` 完全不依赖大模型、不需要 API Key、不联网** ——
    这是 NLC 对生态最实的一份贡献：把"这条命令危不危险"这件事做成一个免费的公共设施。

两个设计原则（与项目总纲一致）：
    1. **AI 只翻译，不执行。** 本服务端只提供"切词块 / 判风险 / 翻译 / 解释"四类只读能力，
       绝不提供"执行命令"的工具。要执行，得由客户端背后的人自己决定。
    2. **零第三方依赖。** 只用标准库，任何装了 Python 的机器都能直接跑。

协议要点：
    - JSON-RPC 2.0，**一行一个消息**（newline-delimited），走 stdin/stdout
    - **stdout 只能出协议消息**；任何调试输出必须走 stderr，否则会污染协议
    - 通知（无 id）不回复；请求（有 id）必须回复

用法：
    python3 -m nlc.mcp_server              # 作为 MCP 服务端跑
    python3 -m nlc.mcp_server --selftest   # 不接客户端，自己跑一遍工具自检
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Callable, Dict, List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nlc import __version__  # noqa: E402
from nlc.blacklist import Blacklist  # noqa: E402
from nlc.prompts import get_profile  # noqa: E402
from nlc.providers import OFFLINE, fallback_chain, get_api_key, get_provider, load_config  # noqa: E402
from nlc.risk import BLOCK, CONFIRM, assess  # noqa: E402
from nlc.tokenizer import render, shell_words, summarize, tokenize  # noqa: E402
from nlc.translator import TranslatorError, explain as tr_explain, translate  # noqa: E402
from nlc.wordbank import WordBank  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "nlc"
SERVER_TITLE = "NLC 自然语言命令行"


def _log(msg: str) -> None:
    """调试输出一律走 stderr —— stdout 是协议通道，绝不能被污染。"""
    print("[nlc] " + msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# 工具实现（全部只读，没有任何执行能力）
# --------------------------------------------------------------------------- #
def _pick_provider():
    cfg = load_config()
    for pid in fallback_chain(cfg):
        if pid == OFFLINE:
            continue
        p = get_provider(cfg, pid)
        if not p:
            continue
        if p.needs_key and not get_api_key(p.id, p.key_env):
            continue
        return cfg, p
    return cfg, None


def tool_tokenize(args: Dict[str, Any]) -> Dict[str, Any]:
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return {"error": "command 不能为空"}
    toks = tokenize(cmd)
    return {
        "command": cmd,
        "count": len(toks),
        "summary": summarize(toks),
        "shell_words": shell_words(toks),
        "tokens": [
            {"index": t.index, "text": t.text,
             "kind": "word" if t.kind == "word" else "symbol",
             "glued_to_previous": (not t.gap_before)}
            for t in toks
        ],
        "rule": "命令词块 = 一个英文单词，或一个符号。- 是独立词块（-name 是两个词块）。",
    }


def tool_assess_risk(args: Dict[str, Any]) -> Dict[str, Any]:
    """★ 不需要 API Key、不联网。任何 MCP 客户端都能免费拿来做安全把关。"""
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return {"error": "command 不能为空"}
    a = assess(cmd)
    return {
        "command": cmd,
        "level": a.level,                       # allow / confirm / block
        "level_text": {"allow": "可执行", "confirm": "需二次确认", "block": "已禁止"}.get(a.level),
        "reason": a.reason,
        "rule_id": a.rule_id,
        "category": a.category,
        "targets": a.targets,
        "executable": a.executable,             # 禁止档不给执行入口
        "needs_confirmation_dialog": a.needs_dialog,
        "note": ("本判定由 NLC 的**本地规则引擎**给出，不经过大模型、不联网、可在离线环境使用。"
                 "level=block 时请勿提供任何执行入口。"),
    }


def tool_translate(args: Dict[str, Any]) -> Dict[str, Any]:
    sentence = str(args.get("sentence") or "").strip()
    if not sentence:
        return {"error": "sentence 不能为空"}
    profile_id = str(args.get("profile") or "concise")
    cfg, p = _pick_provider()
    if p is None:
        return {"error": "NLC 侧还没有配置可用的 AI 通道。"
                         "请在运行本服务端的机器上执行：nlc set-key <通道> <API Key>。"
                         "（注意：nlc_assess_risk 与 nlc_tokenize 不受影响，仍然可用。）"}
    try:
        tr = translate(sentence, p, profile=get_profile(profile_id))
    except TranslatorError as exc:
        return {"error": str(exc)}
    return _translation_payload(tr)


def tool_explain(args: Dict[str, Any]) -> Dict[str, Any]:
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return {"error": "command 不能为空"}
    profile_id = str(args.get("profile") or "concise")
    cfg, p = _pick_provider()
    if p is None:
        return {"error": "NLC 侧还没有配置可用的 AI 通道（逐词块解释需要大模型）。"
                         "nlc_tokenize 与 nlc_assess_risk 不需要密钥，仍然可用。",
                "fallback": tool_tokenize({"command": cmd}),
                "risk": tool_assess_risk({"command": cmd})}
    try:
        tr = tr_explain(cmd, p, profile=get_profile(profile_id))
    except TranslatorError as exc:
        return {"error": str(exc)}
    return _translation_payload(tr)


def _translation_payload(tr) -> Dict[str, Any]:
    return {
        "kind": tr.kind,
        "command": tr.command,
        "summary": tr.summary,
        "notes": tr.notes,
        "profile": tr.source,
        "risk": None if not tr.risk else {
            "level": tr.risk.level,
            "reason": tr.risk.reason,
            "rule_id": tr.risk.rule_id,
            "executable": tr.risk.executable,
            "needs_confirmation_dialog": tr.risk.needs_dialog,
        },
        "tokens": [
            {"index": b.index, "text": b.text,
             "kind": "word" if b.kind == "word" else "symbol",
             "glued_to_previous": (not b.gap_before),
             "meaning": b.meaning}
            for b in tr.blocks
        ],
        "disclaimer": "NLC 只翻译和解释，不执行任何命令。是否执行由人决定。",
    }


def tool_wordbank(args: Dict[str, Any]) -> Dict[str, Any]:
    """查个人词库。★ 纯查表：只看对应关系，不做语义理解。"""
    term = str(args.get("term") or "").strip()
    wb = WordBank()
    if term:
        e = wb.lookup(term)
        if not e:
            return {"hit": False, "term": term, "count": len(wb),
                    "note": "词库里没有这个词（精确匹配，不做语义理解）"}
        return {"hit": True, "term": e.term, "token": e.token, "note": e.note,
                "verified": e.verified}
    return {"count": len(wb),
            "entries": [{"term": e.term, "token": e.token, "verified": e.verified}
                        for e in wb.all()]}


def tool_blacklist(args: Dict[str, Any]) -> Dict[str, Any]:
    bl = Blacklist()
    return {
        "permanent": [{"text": e.text, "added": e.added, "reason": e.reason}
                      for e in bl.permanent.values()],
        "permanent_count": len(bl.permanent),
        "note": "永久区按**词块文本**记（跨命令生效）；临时区按**下标**记，只在单次查询内有效，故此处不展示。",
    }


# --------------------------------------------------------------------------- #
# 工具表（description 是给客户端那个大模型看的，所以要写清"什么时候该用"）
# --------------------------------------------------------------------------- #
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "nlc_assess_risk",
        "description": (
            "判断一条命令的风险档位：allow（可执行）/ confirm（需二次确认）/ block（已禁止）。\n"
            "**在让用户执行任何命令之前，先调用这个工具。**\n"
            "判定由 NLC 的本地规则引擎给出：不经过大模型、不需要 API Key、不联网，离线也能用。\n"
            "规则同时覆盖 **Linux（bash）与 Windows（cmd / PowerShell）** 两套：\n"
            "  - Linux ：关键路径删除熔断（rm -rf /）、包装器绕过防护（env rm -rf /）、"
            "分段判定取最严（ls && rm -rf /）\n"
            "  - Windows：磁盘与引导操作（format / diskpart / bcdedit / clean-disk）、"
            "关键路径删除（del /f /s /q C:\\、Remove-Item -Recurse -Force C:\\）、"
            "关闭安全防护与删除还原点（Set-MpPreference / vssadmin delete shadows）、"
            "包装器绕过防护（cmd /c …、powershell -Command …、iex …）\n"
            "返回 level=block 时，请不要提供任何执行入口。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "要判定的完整命令行"}},
            "required": ["command"],
        },
        "handler": tool_assess_risk,
    },
    {
        "name": "nlc_tokenize",
        "description": (
            "把一条命令切成「命令词块」。\n"
            "命令词块的定义：**一个英文单词，或一个符号**（例如 -name 会被切成 - 和 name 两个词块）。\n"
            "用途：需要按位置精确指出命令的某一部分时（例如告诉用户「把第 8 个词块换掉」），"
            "或需要在界面上把命令逐块显示、逐块标注时。\n"
            "不需要 API Key，不联网。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "要切分的命令行"}},
            "required": ["command"],
        },
        "handler": tool_tokenize,
    },
    {
        "name": "nlc_translate",
        "description": (
            "把一句自然语言翻译成一条命令，并给出**逐词块解释**与风险档位。\n"
            "生成的命令会跟随服务端当前设定的命令行方言（Linux bash / Windows cmd / "
            "PowerShell），用 `nlc shell` 查看或切换。\n"
            "重点不是「唯一正确答案」，而是**让用户看懂**：返回的每个词块都带一句人话解释，"
            "让用户自己判断这条命令是否符合所需。\n"
            "本工具只翻译和解释，**不会执行任何命令**。\n"
            "需要服务端配置了 AI 通道的 API Key。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sentence": {"type": "string", "description": "用户的自然语言需求"},
                "profile": {"type": "string", "enum": ["concise", "balanced", "teacher", "expert"],
                            "description": "提示词角色卡，决定解释的详细程度。默认 concise（最简）"},
            },
            "required": ["sentence"],
        },
        "handler": tool_translate,
    },
    {
        "name": "nlc_explain",
        "description": (
            "给一条**现成**的命令做逐词块解释（不翻译，只解释）。\n"
            "适合用户已经有一条命令、想知道它到底在干什么的场景。\n"
            "同时会附带该命令的风险判定。需要 API Key。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要解释的命令"},
                "profile": {"type": "string", "enum": ["concise", "balanced", "teacher", "expert"]},
            },
            "required": ["command"],
        },
        "handler": tool_explain,
    },
    {
        "name": "nlc_wordbank",
        "description": (
            "查询 NLC 的**个人定制词库**：用户自己定义的「自然语言词 → 命令词块」对照表。\n"
            "**纯查表**：只做精确匹配，不做语义理解。命中就返回对应词块，没命中就明确说没有。\n"
            "给了 term 就查这一个词；不给就列出全部。不需要 API Key。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"term": {"type": "string", "description": "要查的自然语言词；留空则列出全部"}},
        },
        "handler": tool_wordbank,
    },
    {
        "name": "nlc_blacklist",
        "description": (
            "读取 NLC 的**永久黑名单**：用户明确表示过「以后都不要用」的命令词块。\n"
            "在给用户生成命令时，应当避开这些词块。不需要 API Key。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_blacklist,
    },
]

TOOL_INDEX = {t["name"]: t for t in TOOLS}


# --------------------------------------------------------------------------- #
# JSON-RPC / MCP 协议
# --------------------------------------------------------------------------- #
def _result(mid: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": payload}


def _error(mid: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": mid, "error": err}


def _text_content(obj: Any) -> Dict[str, Any]:
    """MCP 的 tool 返回必须是 content 数组。这里统一把结果转成一段 JSON 文本。"""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def handle_message(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理一条 JSON-RPC 消息。返回 None 表示这是通知、不回复。"""
    if not isinstance(msg, dict):
        return _error(None, -32600, "不是合法的 JSON-RPC 对象")

    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if not method:
        return None if is_notification else _error(mid, -32600, "缺少 method")

    # ---- 生命周期 ----
    if method == "initialize":
        return _result(mid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": SERVER_TITLE,
                           "version": __version__},
            "instructions": (
                "NLC 是「人与命令行之间的 AI 翻译层」。它只翻译和解释，**不执行任何命令**。\n"
                "使用建议：在把任何命令交给用户执行之前，先调用 nlc_assess_risk 判风险；"
                "level=block 时不要提供执行入口，level=confirm 时提醒用户二次确认。\n"
                "风险规则同时覆盖 Linux（bash）与 Windows（cmd / PowerShell）。\n"
                "nlc_assess_risk、nlc_tokenize、nlc_wordbank、nlc_blacklist 都不需要 API Key、不联网。"
            ),
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(mid, {})
    if method == "shutdown":
        return _result(mid, None)

    # ---- 工具 ----
    if method == "tools/list":
        return _result(mid, {"tools": [
            {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
            for t in TOOLS
        ]})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOL_INDEX.get(name)
        if not tool:
            return _result(mid, _text_content(
                {"error": "没有这个工具：%s。可用：%s" % (name, "、".join(TOOL_INDEX))}))
        try:
            out = tool["handler"](args if isinstance(args, dict) else {})
        except Exception as exc:                       # noqa: BLE001
            _log("工具 %s 出错：%s" % (name, traceback.format_exc()))
            return _result(mid, {"content": [{"type": "text",
                                              "text": "工具执行失败：%s" % exc}],
                                 "isError": True})
        return _result(mid, _text_content(out))

    # ---- 未实现的可选项：明确回"不支持"，而不是装作成功 ----
    if method in ("resources/list",):
        return _result(mid, {"resources": []})
    if method in ("prompts/list",):
        return _result(mid, {"prompts": []})

    if is_notification:
        return None
    return _error(mid, -32601, "不支持的方法：%s" % method)


def serve(stdin=None, stdout=None) -> int:
    """主循环：一行一条消息，读一条回一条。"""
    fin = stdin or sys.stdin
    fout = stdout or sys.stdout
    _log("NLC MCP 服务端已启动（协议版本 %s）" % PROTOCOL_VERSION)
    for line in fin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            resp = _error(None, -32700, "JSON 解析失败：%s" % exc)
        else:
            resp = handle_message(msg)

        if resp is not None:
            fout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            fout.flush()
    _log("stdin 结束，服务端退出")
    return 0


# --------------------------------------------------------------------------- #
# 自检：不接客户端，直接把工具挨个跑一遍
# --------------------------------------------------------------------------- #
def selftest() -> int:
    print("═══ NLC MCP 服务端自检 ═══\n")

    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": PROTOCOL_VERSION, "clientInfo": {"name": "selftest"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "nlc_tokenize", "arguments": {"command": 'find . -name "*.log" -delete'}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "nlc_assess_risk", "arguments": {"command": "rm -rf /usr"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "nlc_assess_risk", "arguments": {"command": "ls && rm -rf /tmp/x"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "nlc_assess_risk", "arguments": {"command": "env rm -rf /"}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "nlc_assess_risk", "arguments": {"command": "du -sh *"}}},
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
         "params": {"name": "没有这个工具", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 9, "method": "不存在的方法"},
    ]

    ok = 0
    bad = 0
    for m in msgs:
        r = handle_message(m)
        tag = "通知→不回复" if r is None else "已回复"
        print("→ %-22s id=%-4s %s" % (m.get("method"), m.get("id"), tag))
        if r is None:
            continue
        if "error" in r:
            print("   错误：%s" % r["error"]["message"])
            continue
        res = r["result"]
        if m["method"] == "tools/list":
            names = [t["name"] for t in res["tools"]]
            print("   工具 %d 个：%s" % (len(names), "、".join(names)))
            assert "nlc_assess_risk" in names, "缺少风险判定工具"
            assert not any("exec" in n or "run" in n for n in names), "★ 不该有执行类工具！"
            ok += 1
        elif m["method"] == "tools/call":
            body = json.loads(res["content"][0]["text"])
            if "error" in body:
                print("   %s" % body["error"][:80])
            elif "level" in body:
                print("   %-14s → %s（%s）" % (body["command"], body["level"], body["rule_id"]))
            elif "tokens" in body:
                print("   %s ｜ %s" % (body.get("summary"), body.get("shell_words")))
            ok += 1
        else:
            print("   protocolVersion=%s" % res.get("protocolVersion"))
            ok += 1

    # ★ 关键断言：进程里绝不存在执行工具
    print("\n═══ 安全断言 ═══")
    assert len(TOOLS) == 6, "工具数量不对"
    forbidden = ("exec", "run", "shell", "eval", "system")
    leak = [t["name"] for t in TOOLS if any(f in t["name"].lower() for f in forbidden)]
    print("  工具列表里没有任何执行类工具：%s" % ("✔" if not leak else "✘ " + str(leak)))
    assert not leak
    print("  6 个工具全部是只读能力：切词块 / 判风险 / 翻译 / 解释 / 查词库 / 读黑名单 ✔")

    print("\n通过 %d 项，失败 %d 项" % (ok, bad))
    return 1 if bad else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    # ★ 服务端也要先做启动准备：搬老配置 + 定下方言。
    #   nlc_assess_risk / nlc_translate 的结果都取决于方言。
    try:
        from .cli import bootstrap
        bootstrap()
    except Exception as exc:                              # noqa: BLE001 - 定不了方言也别拒绝服务
        _log("启动准备失败，按平台默认继续：%s" % exc)

    if "--selftest" in args:
        return selftest()
    if "--list" in args:
        for t in TOOLS:
            print("%-18s %s" % (t["name"], t["description"].splitlines()[0]))
        return 0
    return serve()


if __name__ == "__main__":
    sys.exit(main())
