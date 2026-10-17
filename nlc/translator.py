# -*- coding: utf-8 -*-
"""NLC 翻译层 —— 三重架构的真正实现（查词 / 命令行 / 对话式脚本）。

⚠️ 铁律（见规则详解第〇节）：
    **AI 只翻译，不执行。** 本模块只产出「命令 + 解释」，
    绝不调用 subprocess / os.system 去跑任何东西。
    风险档位由本地 risk 引擎判定，**不采信大模型的自我评估**。

设计要点：
    1. 命令由大模型产出，但**词块切分与解释对齐由本地分词器负责**
       —— 先把命令本地切成词块，再按「下标」让模型逐个解释，
       这样重复词块（如两个 `|`）也能一一对上，不会串位。
    2. 「重新生成」不是重猜，而是带着用户的否定意见重猜：
       保留未拉黑的片段、只替换被拉黑的片段。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .prompts import PromptProfile, build_system_prompt, get_profile
from .providers import ChannelError, Provider, chat
from .risk import Assessment, assess
from .tokenizer import Token, render, shell_words, tokenize

__all__ = ["Translation", "TokenBlock", "translate", "lookup", "explain",
           "regenerate", "compose", "TranslatorError"]


class TranslatorError(RuntimeError):
    """翻译失败（通道不可用、返回格式不对等）。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class TokenBlock:
    """一个「可点的命令词块」—— 界面上的最小交互单位。"""

    index: int
    text: str
    kind: str            # word / symbol
    gap_before: bool     # False 表示与上一个词块视觉粘连（如 `-` 与 `sh`）
    meaning: str = ""    # 这个词块是什么意思

    @property
    def killable(self) -> bool:
        """是否可以被拉黑。空白之外都能点。"""
        return bool(self.text.strip())


@dataclass
class Translation:
    """一次翻译的完整结果。"""

    kind: str                       # lookup / command / script
    command: str                    # 产出的命令（或脚本）
    summary: str                    # 一句话说明
    blocks: List[TokenBlock] = field(default_factory=list)
    risk: Optional[Assessment] = None
    notes: str = ""
    source: str = ""                # 哪个通道产出的
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_single_token(self) -> bool:
        return len(self.blocks) == 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "command": self.command,
            "summary": self.summary,
            "notes": self.notes,
            "source": self.source,
            "risk": None if not self.risk else {
                "level": self.risk.level, "reason": self.risk.reason,
                "rule_id": self.risk.rule_id, "category": self.risk.category,
                "executable": self.risk.executable,
                "needs_dialog": self.risk.needs_dialog,
            },
            "blocks": [b.__dict__ for b in self.blocks],
        }


# --------------------------------------------------------------------------- #
# 提示词 —— 已全部迁到 nlc/prompts.py 的「角色卡」体系
# --------------------------------------------------------------------------- #
#: 默认角色卡 id。prompts.PROFILES 里还有 balanced / teacher / expert。
#: 用户可以在 ~/.config/nlc/prompts.json 里复制内置卡改成自己的。
DEFAULT_PROFILE = "concise"


# --------------------------------------------------------------------------- #
# JSON 抽取（模型偶尔会加围栏或多余文字）
# --------------------------------------------------------------------------- #
def _extract_json(text: str) -> Dict[str, Any]:
    if not text:
        raise TranslatorError("模型返回为空")
    s = text.strip()

    for candidate in (s,):
        try:
            v = json.loads(candidate)
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass

    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        try:
            v = json.loads(fence.group(1).strip())
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            s = fence.group(1).strip()

    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b > a:
        cand = s[a:b + 1]
        for fixed in (cand,
                      re.sub(r",\s*([}\]])", r"\1", cand.replace("“", '"').replace("”", '"'))):
            try:
                v = json.loads(fixed)
                if isinstance(v, dict):
                    return v
            except json.JSONDecodeError:
                continue
    raise TranslatorError("模型没有返回可解析的 JSON：%s" % s[:160])


def _call(provider: Provider, task: str, user: str,
          profile: Optional[PromptProfile] = None,
          max_tokens: Optional[int] = None) -> Dict[str, Any]:
    """按角色卡组装提示词并调用模型，返回解析后的 JSON。

    Args:
        task: translate / lookup / explain / compose —— 决定任务契约那一段提示词。
        profile: 角色卡；不传则用默认卡（极简模式）。
        max_tokens: 覆盖角色卡里的长度上限。**逐词块解释要用它** ——
            一条 77 个词块的 PowerShell 单行命令，按极简卡的 700 上限必然被截断，
            截断的 JSON 解析不出来，结果就是整条命令一个解释都没有。
    """
    prof = profile or get_profile(DEFAULT_PROFILE)
    system = build_system_prompt(prof, task)
    try:
        out = chat(provider,
                   [{"role": "system", "content": system},
                    {"role": "user", "content": user}],
                   temperature=prof.temperature,
                   max_tokens=max_tokens or prof.max_tokens,
                   json_mode=prof.json_mode)
    except ChannelError as exc:
        raise TranslatorError(str(exc)) from exc
    return _extract_json(out)


# --------------------------------------------------------------------------- #
# 词块与解释的对齐
# --------------------------------------------------------------------------- #
def _align(command: str, items: Optional[Sequence[Dict[str, Any]]] = None) -> List[TokenBlock]:
    """把命令切成词块，并把模型给的逐词解释按**下标**贴上去。

    按下标对齐（而不是按文本）是必须的：`du -sh * | sort -h | head -10` 里
    有两个 `|`，按文本对齐会串位。
    """
    toks: List[Token] = tokenize(command)
    meanings: Dict[int, str] = {}
    for it in items or []:
        try:
            idx = int(it.get("i"))
        except (TypeError, ValueError):
            continue
        m = (it.get("meaning") or "").strip()
        if m:
            meanings[idx] = m

    blocks: List[TokenBlock] = []
    for t in toks:
        blocks.append(TokenBlock(index=t.index, text=t.text, kind=t.kind,
                                 gap_before=t.gap_before, meaning=meanings.get(t.index, "")))
    # 模型漏给解释时，补一句提示，而不是留空让人困惑
    for b in blocks:
        if not b.meaning:
            b.meaning = "（这一块没有拿到解释）"
    return blocks


#: 一次请求最多要多少个词块的解释。
#: ★ Windows 版把它从"不限制"改成"分批"的原因：PowerShell 的单行命令很容易到
#:   70+ 个词块（`Get-ChildItem | ForEach-Object { [PSCustomObject]@{ ... } } | ...`），
#:   一次性索取时模型会写不完 —— 返回的 JSON 被截断 ⇒ 解析失败 ⇒
#:   **整条命令一个解释都拿不到**。分批后每批都是完整可解析的 JSON。
_MEANING_BATCH = 30

#: 每个词块预留的输出预算（token）。中文解释约 10~70 字，60 是留足余量的值。
_TOKENS_PER_ITEM = 60


def _request_meaning(provider: Provider, command: str, toks: Sequence[Token],
                     profile: Optional[PromptProfile] = None) -> List[Dict[str, Any]]:
    """向模型索取逐词块解释。

    词块多的时候**分批索取再做合并**（见 `_MEANING_BATCH` 的说明）。
    某批失败不会让整条命令没解释 —— 失败的那批留空，其余照常返回。
    """
    prof = profile or get_profile(DEFAULT_PROFILE)
    items: List[Dict[str, Any]] = []
    seq = list(toks)

    for start in range(0, len(seq), _MEANING_BATCH):
        chunk = seq[start:start + _MEANING_BATCH]
        listing = "\n".join("  [%d] %s  （%s）" % (
            t.index, t.text, "单词" if t.kind == "word" else "符号") for t in chunk)
        head = ("命令：\n%s\n\n" % command) if start == 0 else \
               ("同一条命令（这里只节选一部分词块）：\n%s\n\n" % command)
        user = ("%s它被切分成 %d 个命令词块，下面只是其中的第 %d~%d 个：\n%s\n\n"
                "请**只**为下面列出的这些下标各写一句解释。"
                % (head, len(seq), chunk[0].index, chunk[-1].index, listing))
        budget = max(prof.max_tokens, 240 + _TOKENS_PER_ITEM * len(chunk))
        try:
            data = _call(provider, "explain", user, profile, max_tokens=budget)
        except TranslatorError:
            if len(seq) <= _MEANING_BATCH:
                raise                       # 只有一批还失败 ⇒ 本来就是拿不到，按老规矩处理
            continue                        # 分批时某一批失败不影响其它批
        got = data.get("items")
        if isinstance(got, list):
            items.extend(got)
        elif len(seq) <= _MEANING_BATCH:
            raise TranslatorError("解释接口没有返回 items 数组")
    return items


# --------------------------------------------------------------------------- #
# 片段计算（给「重新生成」用）
# --------------------------------------------------------------------------- #
def split_kept_killed(command: str, killed: Sequence[int]) -> Dict[str, Any]:
    """按被拉黑的下标，把命令拆成「保留片段」和「待替换片段」。"""
    toks = tokenize(command)
    killed_set = set(killed)
    kept_parts: List[str] = []
    killed_parts: List[str] = []
    for t in toks:
        bucket = killed_parts if t.index in killed_set else kept_parts
        if bucket and not t.gap_before:
            bucket[-1] += t.text
        else:
            bucket.append(t.text)
    return {
        "tokens": toks,
        "killed_indices": sorted(killed_set),
        "kept_text": " ".join(p for p in kept_parts if p.strip()),
        "killed_text": " ".join(p for p in killed_parts if p.strip()),
        "killed_tokens": [t.text for t in toks if t.index in killed_set],
        "all_killed": bool(toks) and len(killed_set) >= len(toks),
    }


def _kill_runs(toks: Sequence[Token], killed: Sequence[int]) -> List[List[str]]:
    """把被拉黑的下标归并成「连续的片段」，每段是一个词块文本序列。

    为什么必须归并：用户点掉的往往不连续，例如 `-n 10` 里点掉 `-` 和 `10`、
    却漏点了中间的 `n`。这时逐个词块要求"不得出现"，`10` 这种常见数字会大面积误伤；
    归并成片段才是用户真正想换掉的整体。
    """
    ks = set(killed)
    runs: List[List[Token]] = []
    cur: List[Token] = []
    for t in toks:
        if t.index in ks:
            cur.append(t)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    # 只保留长度 ≥2 的片段：单个符号或单个短数字（一个 `-`、一个 `a`）
    # 在任何命令里都可能合法出现，拿它做"不得出现"的判据会大量误报。
    # 这是已知取舍，写在文档里。
    return [[t.text for t in r] for r in runs if len(r) >= 2]


def _contains_subseq(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """needle 是否作为**连续子序列**出现在 haystack 里（按词块比，不按字符串比）。

    ★ 必须按词块比：早期版本用子串匹配，结果 `10` 会命中 `'1,10p'` 里的 `10`，
    把一次成功的替换误判成失败。
    """
    n = len(needle)
    if n == 0 or n > len(haystack):
        return False
    for i in range(len(haystack) - n + 1):
        if list(haystack[i:i + n]) == list(needle):
            return True
    return False


def validate_regeneration(old_command: str, new_command: str, killed: Sequence[int]) -> List[str]:
    """校验新命令是否真的避开了被拉黑的片段。返回**仍然残留**的片段（空 = 通过）。

    这道校验是必要的：实测中模型会嘴上答应、实际原封不动地把旧写法返回。
    不校验，「重新生成」按钮就只是个摆设。
    """
    old_toks = tokenize(old_command)
    if not old_toks:
        return []
    ks = set(killed)
    # 整条全被点掉 ⇒ 没有"必须保留"的部分，无从谈残留，直接放行
    if len(ks) >= len(old_toks):
        return []
    new_texts = [t.text for t in tokenize(new_command)]
    return [" ".join(seq) for seq in _kill_runs(old_toks, killed)
            if _contains_subseq(new_texts, seq)]


# --------------------------------------------------------------------------- #
# 对外接口：三重架构
# --------------------------------------------------------------------------- #
def _pick_provider(provider: Optional[Provider]) -> Provider:
    if provider is None:
        raise TranslatorError("没有可用的 AI 通道，请先填 API Key（nlc set-key <通道> <key>）")
    return provider


def _annotated_listing(toks: Sequence[Token], killed: Sequence[int]) -> str:
    ks = set(killed)
    return "\n".join("  [%2d] %-12s %s" % (t.index, t.text, "KILL（必须换掉）" if t.index in ks
                                          else "KEEP（必须保留）") for t in toks)


def _translate_once(provider: Provider, user: str,
                    profile: Optional[PromptProfile] = None) -> Dict[str, Any]:
    data = _call(provider, "translate", user, profile)
    cmd = (data.get("command") or "").strip().strip("`")
    if not cmd:
        raise TranslatorError("翻译结果里没有 command 字段")
    return {"command": cmd, "raw": data}


def _attach_meanings(provider: Provider, cmd: str,
                     profile: Optional[PromptProfile] = None) -> List[TokenBlock]:
    blocks = _align(cmd)
    try:
        items = _request_meaning(provider, cmd,
                                 [Token(b.index, b.text, b.kind, b.gap_before) for b in blocks],
                                 profile)
        return _align(cmd, items)
    except TranslatorError:
        return blocks      # 解释拿不到不算致命，命令本身还能用


def lookup(term: str, provider: Provider, profile: Optional[PromptProfile] = None,
           with_meaning: bool = True) -> Translation:
    """① 查词：一个词 → 一个命令词块 + 词义解释。"""
    p = _pick_provider(provider)
    data = _call(p, "lookup", "用户查的词：%s" % term.strip(), profile)
    cmd = (data.get("command") or "").strip()
    if not cmd:
        raise TranslatorError("查词结果里没有 command 字段")

    # 查词只取一个命令词块：若模型给了整条命令，取第一个词块
    blocks = _align(cmd)
    if len(blocks) > 1:
        cmd = blocks[0].text
        blocks = _align(cmd)
    if not blocks[0].meaning or blocks[0].meaning.startswith("（这一块"):
        blocks[0].meaning = (data.get("summary") or "").strip() or blocks[0].meaning

    return Translation(kind="lookup", command=cmd,
                       summary=(data.get("summary") or "").strip(),
                       blocks=blocks, risk=assess(cmd),
                       notes=(data.get("notes") or "").strip(),
                       source=p.name or p.id, raw=data)


def translate(sentence: str, provider: Provider,
              exclude_texts: Sequence[str] = (),
              with_meaning: bool = True,
              profile: Optional[PromptProfile] = None) -> Translation:
    """② 命令行：一句话 → 命令 + 整句解释。

    Args:
        exclude_texts: 需要避开的词块文本（来自黑名单的临时区/永久区）。
        with_meaning: 是否顺带请求逐词块解释（多一次调用）。
    """
    p = _pick_provider(provider)
    parts = ["用户需求：%s" % sentence.strip()]
    if exclude_texts:
        parts.append("\n【黑名单】以下词块被用户明确否定了，本轮**绝对不要使用**：%s"
                     % "、".join("`%s`" % t for t in exclude_texts))

    res = _translate_once(p, "\n".join(parts), profile)
    cmd, data = res["command"], res["raw"]

    blocks = _attach_meanings(p, cmd, profile) if with_meaning else _align(cmd)
    return Translation(kind="command", command=cmd,
                       summary=(data.get("summary") or "").strip(),
                       blocks=blocks, risk=assess(cmd),
                       notes=(data.get("notes") or "").strip(),
                       source=p.name or p.id, raw=data)


def regenerate(sentence: str, base: Translation, killed: Sequence[int],
               provider: Provider, exclude_texts: Sequence[str] = (),
               profile: Optional[PromptProfile] = None) -> Translation:
    """按用户点掉的词块重新生成，**并校验结果**。

    - 点了若干块：保留未点中的、替换点中的（块用 KEEP/KILL 标注后交给模型）
    - 一块都没点：整条全部换掉（等于"这条命令整个不对"）

    实测发现模型会"嘴上答应、实际原文返回"，所以这里必须校验；
    第一次不过就用更强硬的措辞重试一次，仍不过则在 notes 里如实告知用户。
    """
    p = _pick_provider(provider)
    toks = tokenize(base.command)
    killed = [i for i in killed if 0 <= i < len(toks)]
    all_killed = (not killed) or len(set(killed)) >= len(toks)

    common = ""
    if exclude_texts:
        common += ("\n【黑名单】以下词块被用户否定了，**绝对不要使用**：%s"
                   % "、".join("`%s`" % t for t in exclude_texts))

    if all_killed:
        user = ("用户需求：%s\n\n"
                "【重新生成】上一条命令被用户**整个否决**了：\n%s\n"
                "请换一条**思路明显不同**的写法来实现同样的目的，不要在原命令上做微调。"
                "%s" % (sentence.strip(), base.command, common))
    else:
        user = ("用户需求：%s\n\n"
                "上一条命令是：\n%s\n\n"
                "它的词块及其处置要求如下：\n%s\n\n"
                "请给出新的完整命令，满足：\n"
                "1. 标 KEEP 的词块必须原样保留；\n"
                "2. 标 KILL 的词块**必须换成别的写法**，新命令里不允许出现它们；\n"
                "3. 如果 KILL 与 KEEP 交错在一起、无法单独替换，就把交错的那一整段一起换掉；\n"
                "4. 新命令实现的目的必须和原来一样。%s"
                % (sentence.strip(), base.command, _annotated_listing(toks, killed), common))

    res = _translate_once(p, user, profile)
    cmd, data = res["command"], res["raw"]
    residual = validate_regeneration(base.command, cmd, killed)

    if residual:
        retry = (user + "\n\n【上一次尝试不合格】你上一次给出的命令是：\n%s\n"
                 "它里面**仍然出现了**这些被要求换掉的片段：%s\n"
                 "请重新给出一个命令，务必让这些片段**一个都不出现**。"
                 % (cmd, "、".join("`%s`" % r for r in residual)))
        try:
            res2 = _translate_once(p, retry, profile)
            residual2 = validate_regeneration(base.command, res2["command"], killed)
            if len(residual2) < len(residual):
                cmd, data, residual = res2["command"], res2["raw"], residual2
        except TranslatorError:
            pass

    blocks = _attach_meanings(p, cmd, profile)
    notes = (data.get("notes") or "").strip()
    if residual:
        notes = ("⚠️ 这一次没能完全避开你点掉的片段（仍残留：%s）。"
                 "可以再点一次「重新生成」，或者干脆把它们全部点掉重来。\n" % "、".join(residual)) + notes

    return Translation(kind="command", command=cmd,
                       summary=(data.get("summary") or "").strip(),
                       blocks=blocks, risk=assess(cmd), notes=notes,
                       source=p.name or p.id, raw=data)


def explain(command: str, provider: Provider,
            profile: Optional[PromptProfile] = None) -> Translation:
    """给一条**现成**的命令逐词块解释（对应参考里的「命令模式」）。"""
    p = _pick_provider(provider)
    toks = tokenize(command)
    if not toks:
        raise TranslatorError("这条命令是空的")
    items = _request_meaning(p, command, toks, profile)
    blocks = _align(command, items)
    return Translation(kind="command", command=command, summary="",
                       blocks=blocks, risk=assess(command),
                       source=p.name or p.id)


def compose(commands: Sequence[str], provider: Provider, title: str = "",
            profile: Optional[PromptProfile] = None) -> Translation:
    """③ 对话式脚本：把若干条命令拼成一个脚本。

    ⚠️ 按规则：**不要求逻辑连贯**，用户可能就是想把几条不相干的命令放一起跑。
       这里绝不替用户"优化"或"过滤"，只在 notes 里提示风险。
    """
    p = _pick_provider(provider)
    cmds = [c.strip() for c in commands if c and c.strip()]
    if not cmds:
        raise TranslatorError("没有选中任何命令")
    user = ("标题（可为空）：%s\n\n待组合的 %d 条命令：\n%s"
            % (title, len(cmds), "\n".join("%d. %s" % (i + 1, c) for i, c in enumerate(cmds))))
    data = _call(p, "compose", user, profile)

    script = (data.get("command") or "").strip().strip("`")
    if not script:
        # 模型失败时的本地兜底：直接拼起来，保证功能不中断
        script = "\n".join(
            ("" if i == 0 else 'echo "──────"\n') + c for i, c in enumerate(cmds))

    # 脚本的整体风险 = 各段取最严（本地判定，不采信模型）
    worst = assess(script)

    return Translation(kind="script", command=script,
                       summary=(data.get("summary") or "").strip(),
                       blocks=[], risk=worst,
                       notes=(data.get("notes") or "").strip(),
                       source=p.name or p.id, raw=data)


# --------------------------------------------------------------------------- #
# 展示辅助
# --------------------------------------------------------------------------- #
def render_blocks(tr: Translation, killed: Sequence[int] = (), mark: str = "[X]") -> str:
    """把词块渲染成一行，被拉黑的加标记（给命令行界面用）。"""
    ks = set(killed)
    out: List[str] = []
    for b in tr.blocks:
        txt = "%s%s" % (mark, b.text) if b.index in ks else b.text
        if out and not b.gap_before:
            out[-1] += txt
        else:
            out.append(txt)
    return " ".join(out)
