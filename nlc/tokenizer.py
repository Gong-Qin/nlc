# -*- coding: utf-8 -*-
"""NLC 命令词块切分器。

规则（用户定稿，2026-10-02）：
    命令词块 = 一个英文单词，或一个符号。
    扫描字符串：连续的一段字母/数字/下划线 ⇒ 算一个词块；
    其余每一个字符，各自单独算一个词块；重复出现的各自独立计数。

由此得出的关键行为：
    find            -> 1 个词块
    -name           -> 2 个词块（`-` 和 `name`）
    --max-depth=1   -> 7 个词块
    "*.log"         -> 5 个词块
    //name          -> 3 个词块

本模块是纯逻辑，不依赖任何第三方库，可单独单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

__all__ = ["Token", "tokenize", "shell_words", "render", "summarize", "split_segments"]

# 空白字符不算词块，只作为分隔信息记录
_WS = re.compile(r"\s")


@dataclass(frozen=True)
class Token:
    """一个命令词块。

    Attributes:
        index:       在整条命令里的下标（从 0 开始）。★ 拉黑必须按它来，不能按 text。
        text:        词块原文。
        kind:        "word"（英文单词/数字/中文词）或 "symbol"（符号）。
        gap_before:  前面是否有空白。用于界面「视觉粘连」：
                     False 表示紧贴前一个词块（如 `-` 与 `sh`），显示时间为 0 间距。
    """

    index: int
    text: str
    kind: str
    gap_before: bool

    @property
    def is_symbol(self) -> bool:
        return self.kind == "symbol"

    def __str__(self) -> str:  # pragma: no cover - 仅调试
        return self.text


def _is_word_char(ch: str) -> bool:
    """判断一个字符是否算「词字符」（会与相邻词字符合并成一个词块）。

    - ASCII：字母、数字、下划线 ⇒ 词字符
    - 非 ASCII：字母/数字/汉字等 isalnum() 为真的 ⇒ 词字符（避免中文路径被拆成单字）
    - 其余（含中文标点、各类运算符）⇒ 符号，各自独立成词块
    """
    if ch == "_":
        return True
    try:
        return ch.isalnum()
    except AttributeError:  # pragma: no cover - 防御
        return False


def tokenize(command: str) -> List[Token]:
    """把一条命令切成命令词块列表。

    Args:
        command: 命令行原文。

    Returns:
        Token 列表，index 已按顺序填好。
    """
    tokens: List[Token] = []
    buf: List[str] = []
    pending_gap = False  # 当前位置之前是否出现过空白

    def flush() -> None:
        nonlocal pending_gap
        if buf:
            text = "".join(buf)
            tokens.append(Token(len(tokens), text, "word", pending_gap))
            buf.clear()
            # ★ 词块吐出去之后要复位：紧跟其后的字符与它是相连的
            #   （`important` 与 `.txt`、`du` 与 `-sh`），中间并没有空白。
            #   不复位会让 `important.txt` 在「壳词」层被拆成两个参数。
            pending_gap = False

    for ch in command:
        if _is_word_char(ch):
            buf.append(ch)
            continue
        # 不是词字符：先把攒着的词块吐出去
        flush()
        if _WS.match(ch):
            pending_gap = True
            continue
        # 符号：每个字符单独一个词块
        tokens.append(Token(len(tokens), ch, "symbol", pending_gap))
        # ★ 紧跟在符号后面的字符与本符号是相连的（`-` 与 `rf`、`/` 与 `usr`），
        #   所以这里必须置 False。置 True 会让 `-rf`、`$HOME`、`/usr` 在
        #   「壳词」层被拆散，风险引擎就再也认不出它们了。
        pending_gap = False

    flush()
    return tokens


def render(tokens: List[Token], killed: List[int] | None = None, keep_gap: bool = True) -> str:
    """把词块列表还原成命令字符串（用于展示/执行）。

    Args:
        tokens: 词块列表。
        killed: 需要标记为「已拉黑」的下标列表，会加上删除线标记（仅供终端预览）。
        keep_gap: True 则按原始空白还原；False 则全部用单空格拼接。
    """
    killed_set = set(killed or [])
    parts: List[str] = []
    for t in tokens:
        txt = t.text
        if t.index in killed_set:
            txt = "[%s]" % txt
        if not parts:
            parts.append(txt)
        elif keep_gap and not t.gap_before:
            parts[-1] = parts[-1] + txt          # 视觉粘连：直接贴上
        else:
            parts.append(txt)
    return " ".join(parts)


def summarize(tokens: List[Token]) -> str:
    """给人看的一行摘要，用于界面右上角的「N 个词块」。"""
    words = sum(1 for t in tokens if t.kind == "word")
    syms = len(tokens) - words
    return "%d 个词块（%d 单词 + %d 符号）" % (len(tokens), words, syms)


def shell_words(tokens: List[Token]) -> List[str]:
    """把词块按「空白边界」重新聚成 shell 参数（空格分隔的字段）。

    为什么需要这一层：严格切分把 `-R` 拆成 `-` 和 `R`、把 `"*.log"` 拆成 5 个词块。
    这对「按词块拉黑」是正确的，但风险引擎需要看「一个完整的参数」才能判
    「这是不是 -R」「这是不是 /usr」。
    ⇒ 词块层给界面用，壳词层给风险引擎用，两者都不牺牲。

    例：`rm -rf /usr` ⇒ ["rm", "-rf", "/usr"]
    """
    out: List[str] = []
    for t in tokens:
        if not out or t.gap_before:
            out.append(t.text)
        else:
            out[-1] += t.text
    return out


def split_segments(tokens: List[Token], shell: str = "auto") -> List[List[Token]]:
    """按 `;` `&&` `||` `|` `&` 把词块切成若干「子命令段」。

    风险判定必须逐段做，再取最严的一档 —— 否则 `ls && rm -rf /tmp/x`
    会被前半段的 `ls` 蒙混过去。

    引号内的分隔符不算分隔符（简单状态机，不解析转义）。

    ★ Windows 方言的转义符也要认（这是 Windows 版新加的）：
        - **cmd**：`^` 是转义符 ⇒ `echo a ^& b` 里的 `&` 是**字面量**，
          不是"顺序执行"，**不能**在这里切开。若不处理，正常写法会被误判成
          "由多段组成、其中一段危险"。
        - **PowerShell**：反引号是转义符，同理。
        - **bash**：保持原样不动（转义在引号状态机之外，历史行为已被 138 项自测锁定）。

    ★ 还要认**花括号的层级**（`{ }`）：
        PowerShell 与 cmd 的脚本块里 `;` 是"语句分隔"，不是"把命令行拆开"。
        例：`Get-ChildItem | ForEach-Object { 名 = $_.Name; 大小 = ... }`
        若在这里按 `;` 切开，后半段会变成一条以 `大小` 开头的"命令"，
        被判成"未知命令 ⇒ 需确认" —— 一条完全只读的管道被误报成红色。

        ⚠️ 只对 `{ }` 网开一面，**圆括号不网开一面**：
        `(... | rm -rf /)` 里的危险东西必须还能被切出来；
        `|` 也一律照切不误 —— 管道两侧才是命令真正出现的地方。
    """
    from .platform import active_shell      # 延迟导入，避免模块级循环

    sk = active_shell() if shell in (None, "", "auto") else shell
    escape_char = {"cmd": "^", "powershell": "`"}.get(sk)

    # ★ 注意 `&&` 被切分器拆成两个 `&` 符号，`||` 拆成两个 `|`，
    #   所以分隔符表必须包含单个 `&` / `|`，否则 `ls && rm -rf /`
    #   会被当成一整段、以 `ls` 为准而误判成安全。
    seps = {";", "|", "&"}
    segments: List[List[Token]] = []
    cur: List[Token] = []
    in_single = False
    in_double = False
    escaped = False          # 上一个词块是不是转义符
    brace = 0                # 花括号嵌套深度

    for t in tokens:
        if escaped:
            escaped = False
            cur.append(t)    # 被转义的字符：原样留下，且绝不当作分隔符
            continue
        if escape_char and t.text == escape_char and not in_single and not in_double:
            escaped = True
            cur.append(t)
            continue
        if t.text == "'" and not in_double:
            in_single = not in_single
        elif t.text == '"' and not in_single:
            in_double = not in_double
        elif t.text == "{" and not in_single and not in_double:
            brace += 1
        elif t.text == "}" and not in_single and not in_double:
            brace = max(0, brace - 1)
        elif t.text in seps and not in_single and not in_double:
            # `|` 永远切开（管道两侧就是命令）；`;` / `&` 在花括号里不切
            if t.text != "|" and brace > 0:
                cur.append(t)
                continue
            segments.append(cur)
            cur = []
            continue
        cur.append(t)
    segments.append(cur)
    return [s for s in segments if s]
