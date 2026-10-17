# -*- coding: utf-8 -*-
"""NLC 个人定制词库。

规则（用户定稿）：
    1. 用户自己定义一套「自然语言词 → 命令词块」的对照表。
    2. **严格一对一**：一个自然语言词只能对应一个命令词块（词条不许重名）。
    3. **执行时只考虑对应，不考虑具体含义** —— 纯查表，不做语义理解、不做推理。
    4. 提供「分享码」与「.nlcwords 文件」两种分享方式（远景功能已落地）。

与「翻译开关」的关系（见规则详解 §4.2）：
    翻译**开着**：走 AI 翻译，词库命中时额外标注「来自个人词库」
    翻译**关着**：只查表、不调 AI；**回车直接执行**对应命令

⚠️ 安全底线：**即使翻译关着、即使是自己填的词库，危险命令仍然要过风险判定。**
    这道闸门由调用方（上层）负责，本模块只负责「查表」这件事，不碰风险引擎。
"""

from __future__ import annotations

import base64
import json
import re
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .platform import CONFIG_DIR

__all__ = ["WordEntry", "WordBank", "WordBankError", "SHARE_PREFIX"]

WORDBANK_PATH = CONFIG_DIR / "wordbank.json"

#: 分享码前缀。带版本号是为了以后格式变了还能认出来。
SHARE_PREFIX = "NLC1-"


class WordBankError(RuntimeError):
    """词库操作出错（重名、格式不对等）。"""


@dataclass
class WordEntry:
    """一条词库记录：一个自然语言词 → 一个命令词块。"""

    term: str                 # 自然语言词（唯一）
    token: str                # 命令词块（一个英文单词或一个符号）
    note: str = ""
    verified: bool = True     # 自己加的 = 已审核；**导入的默认 False**
    added: str = ""


class WordBank:
    """个人词库。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else WORDBANK_PATH
        self.entries: Dict[str, WordEntry] = {}
        self.load()

    # ------------------------------------------------------------------ #
    # 增删改查
    # ------------------------------------------------------------------ #
    def add(self, term: str, token: str, note: str = "", verified: bool = True) -> WordEntry:
        """加一条。★ 严格一对一：词条重名直接报错，不覆盖。

        为什么不允许覆盖：一个自然语言词对应两个命令词块，就违背了
        "只考虑对应、不考虑含义"的纯查表语义 —— 那时该给哪个？
        """
        t = (term or "").strip()
        k = (token or "").strip()
        if not t:
            raise WordBankError("自然语言词不能为空")
        if not k:
            raise WordBankError("对应的命令词块不能为空")
        if t in self.entries:
            raise WordBankError("「%s」已经定义过了（现在对应 `%s`）。"
                                "一个词只能对应一个命令词块；要改就先删掉原来那条。"
                                % (t, self.entries[t].token))
        e = WordEntry(term=t, token=k, note=note.strip(), verified=verified,
                      added=datetime.now().strftime("%Y-%m-%d"))
        self.entries[t] = e
        self.save()
        return e

    def update(self, term: str, token: Optional[str] = None,
               note: Optional[str] = None) -> WordEntry:
        if term not in self.entries:
            raise WordBankError("词库里没有「%s」" % term)
        e = self.entries[term]
        if token is not None and token.strip():
            e.token = token.strip()
        if note is not None:
            e.note = note.strip()
        self.save()
        return e

    def remove(self, term: str) -> bool:
        if term in self.entries:
            del self.entries[term]
            self.save()
            return True
        return False

    def lookup(self, term: str) -> Optional[WordEntry]:
        """★ 纯查表：只做精确匹配，不做任何语义理解或模糊匹配。

        前导/尾随空格忽略，其余一律严格相等 —— 这是"只考虑对应"的直接落地。
        """
        return self.entries.get((term or "").strip())

    def __len__(self) -> int:
        return len(self.entries)

    def all(self) -> List[WordEntry]:
        return sorted(self.entries.values(), key=lambda e: e.added or "", reverse=True)

    # ------------------------------------------------------------------ #
    # 分享：分享码
    # ------------------------------------------------------------------ #
    def export_code(self) -> str:
        """把整个词库压成一段分享码。"""
        payload = json.dumps(
            {"v": 1, "words": [{"t": e.term, "k": e.token, "n": e.note} for e in self.entries.values()]},
            ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        b64 = base64.urlsafe_b64encode(zlib.compress(payload, 9)).decode("ascii").rstrip("=")
        return SHARE_PREFIX + b64

    def import_code(self, code: str, overwrite: bool = False) -> Tuple[int, List[str]]:
        """导入分享码。返回 (成功条数, 跳过原因列表)。

        ★ 导入的词条一律标记为**未经审核**：第一次执行时强制弹确认。
        """
        raw = (code or "").strip().replace("\n", "").replace(" ", "")
        if raw.startswith(SHARE_PREFIX):
            raw = raw[len(SHARE_PREFIX):]
        pad = "=" * (-len(raw) % 4)
        try:
            blob = zlib.decompress(base64.urlsafe_b64decode(raw + pad))
            data = json.loads(blob.decode("utf-8"))
        except Exception as exc:
            raise WordBankError("分享码看不懂或已损坏：%s" % exc) from exc

        words = data.get("words")
        if not isinstance(words, list):
            raise WordBankError("分享码里没有词条")

        ok, skipped = 0, []
        for w in words:
            term, token = (w.get("t") or "").strip(), (w.get("k") or "").strip()
            if not term or not token:
                continue
            if term in self.entries and not overwrite:
                skipped.append("「%s」已存在（保留原值）" % term)
                continue
            self.entries[term] = WordEntry(
                term=term, token=token, note=(w.get("n") or "").strip(),
                verified=False,                     # ★ 导入的都不算已审核
                added=datetime.now().strftime("%Y-%m-%d"))
            ok += 1
        if ok:
            self.save()
        return ok, skipped

    # ------------------------------------------------------------------ #
    # 分享：文件
    # ------------------------------------------------------------------ #
    FILE_HEADER = "# NLC 个人词库 (.nlcwords)\n# 一行一条：自然语言词 = 命令词块\n"

    def export_text(self) -> str:
        lines = [self.FILE_HEADER]
        for e in self.all():
            lines.append("%s = %s%s" % (e.term, e.token, ("   # " + e.note) if e.note else ""))
        return "\n".join(lines) + "\n"

    def export_file(self, path: Path) -> Path:
        p = Path(path)
        p.write_text(self.export_text(), encoding="utf-8")
        return p

    def import_text(self, text: str, overwrite: bool = False) -> Tuple[int, List[str]]:
        ok, skipped = 0, []
        for lineno, line in enumerate((text or "").splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            note = ""
            if "#" in s:
                s, note = s.split("#", 1)
                s, note = s.strip(), note.strip()
            if "=" not in s:
                skipped.append("第 %d 行没有 `=`，跳过" % lineno)
                continue
            term, token = s.split("=", 1)
            term, token = term.strip(), token.strip()
            if not term or not token:
                skipped.append("第 %d 行不完整，跳过" % lineno)
                continue
            if term in self.entries and not overwrite:
                skipped.append("「%s」已存在（保留原值）" % term)
                continue
            self.entries[term] = WordEntry(term=term, token=token, note=note,
                                           verified=False,
                                           added=datetime.now().strftime("%Y-%m-%d"))
            ok += 1
        if ok:
            self.save()
        return ok, skipped

    def import_file(self, path: Path, overwrite: bool = False) -> Tuple[int, List[str]]:
        return self.import_text(Path(path).read_text(encoding="utf-8"), overwrite)

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
        for d in data.get("words", []):
            try:
                e = WordEntry(**{k: v for k, v in d.items() if k in WordEntry.__dataclass_fields__})
            except TypeError:
                continue
            if e.term and e.token:
                self.entries[e.term] = e

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data: Dict[str, Any] = {"version": 1,
                                "words": [asdict(e) for e in self.entries.values()]}
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.path

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    _TOKEN_OK = re.compile(r"^[A-Za-z0-9_]+$|^.$")

    @classmethod
    def looks_like_token(cls, token: str) -> bool:
        """这个字符串像不像一个「命令词块」（一个英文单词，或一个符号）。

        仅做提醒用 —— 因为规则里"符号"的范围没有硬性白名单，
        所以即使不像也允许保存，只是提示一下。
        """
        return bool(cls._TOKEN_OK.match((token or "").strip()))
