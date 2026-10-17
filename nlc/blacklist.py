# -*- coding: utf-8 -*-
"""NLC 命令词块黑名单（临时区 / 永久区）。

★ 两个区的标识方式**故意不同**，这不是随手定的：

| 区域 | 用什么记 | 为什么 |
|---|---|---|
| **临时区** | **下标**（第 8 个词块） | 一次查询里 `|` 可能出现两次，只有下标能分清用户点的是哪一个 |
| **永久区** | **词块文本**（`head`） | 跨命令时"第 8 个词块"毫无意义；只有文本能跨命令生效 |

⇒ 「临时升永久」这一步，就是把「下标」翻译成「文本」。反过来做不到（文本无法还原成位置）。

生命周期：
    临时区：开始新的一次查询时自动清空
    永久区：用户手动移除才消失
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .platform import CONFIG_DIR

__all__ = ["SessionKill", "PermEntry", "Blacklist"]

BLACKLIST_PATH = CONFIG_DIR / "blacklist.json"


@dataclass
class SessionKill:
    """临时区的一条记录：绑定在本次查询上。"""

    index: int          # 词块下标（★ 临时区按位置）
    text: str           # 词块文本（升永久区时要用）
    command: str = ""   # 它出自哪条命令
    reason: str = ""


@dataclass
class PermEntry:
    """永久区的一条记录：跨会话生效。"""

    text: str           # ★ 永久区按文本
    reason: str = ""
    added: str = ""


class Blacklist:
    """临时区 + 永久区的黑名单。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else BLACKLIST_PATH
        self.session_query: str = ""
        self.session: List[SessionKill] = []
        self.permanent: Dict[str, PermEntry] = {}
        self.load()

    # ------------------------------------------------------------------ #
    # 临时区
    # ------------------------------------------------------------------ #
    def start_query(self, query: str) -> None:
        """开始一次新查询 —— 临时区自动清空（这是临时区的定义）。"""
        if query != self.session_query:
            self.session_query = query
            self.session = []

    def kill(self, index: int, text: str, command: str = "", reason: str = "") -> None:
        """在临时区拉黑一个词块。重复拉黑同一个下标不会产生两条记录。"""
        for k in self.session:
            if k.index == index:
                k.text, k.reason = text, reason or k.reason
                return
        self.session.append(SessionKill(index=index, text=text, command=command, reason=reason))

    def unkill(self, index: int) -> None:
        self.session = [k for k in self.session if k.index != index]

    def session_indices(self) -> List[int]:
        return sorted(k.index for k in self.session)

    def session_texts(self) -> List[str]:
        return [k.text for k in self.session]

    def clear_session(self) -> None:
        self.session = []
        self.session_query = ""

    def is_all_killed(self, token_count: int) -> bool:
        """本条命令是否被整条否决 —— 对应「一个都不点 ⇒ 全部换掉」中的一种极端。

        注意与「一个都没点」的区别：一个都没点也应该整条换掉。
        调用方判断"该不该整条换掉"的条件是：`len(indices) == 0 or is_all_killed(n)`。
        """
        return token_count > 0 and len(self.session) >= token_count

    # ------------------------------------------------------------------ #
    # 永久区
    # ------------------------------------------------------------------ #
    def ban(self, text: str, reason: str = "") -> None:
        t = (text or "").strip()
        if not t:
            return
        self.permanent[t] = PermEntry(
            text=t, reason=reason,
            added=self.permanent.get(t, PermEntry(t)).added or datetime.now().strftime("%Y-%m-%d"))
        self.save()

    def unban(self, text: str) -> bool:
        if text in self.permanent:
            del self.permanent[text]
            self.save()
            return True
        return False

    def promote(self, indices: Optional[List[int]] = None) -> List[str]:
        """把临时区的词块升级为永久拉黑（下标 ⇒ 文本）。返回被升级的文本列表。"""
        picks = ([k for k in self.session if k.index in set(indices)]
                 if indices is not None else list(self.session))
        moved: List[str] = []
        for k in picks:
            if k.text.strip():
                self.ban(k.text, k.reason)
                moved.append(k.text)
        # 升级完就把这些从临时区移走（它们的语义已经上升到永久区）
        if moved:
            self.session = [k for k in self.session if k.text not in set(moved)]
        return moved

    # ------------------------------------------------------------------ #
    # 给翻译层用
    # ------------------------------------------------------------------ #
    def exclude_texts(self, include_session: bool = True) -> List[str]:
        """要写进提示词的"绝对不要用"清单：永久区 + （可选）临时区。"""
        out = list(self.permanent.keys())
        if include_session:
            for t in self.session_texts():
                if t not in out:
                    out.append(t)
        return out

    # ------------------------------------------------------------------ #
    # 存取
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for d in data.get("permanent", []):
            try:
                e = PermEntry(**{k: v for k, v in d.items() if k in PermEntry.__dataclass_fields__})
                self.permanent[e.text] = e
            except TypeError:
                continue

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data: Dict[str, Any] = {
            "version": 1,
            "permanent": [asdict(e) for e in self.permanent.values()],
        }
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.path
