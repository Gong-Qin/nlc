# -*- coding: utf-8 -*-
"""NLC（自然语言命令行）—— 在人与命令行之间加一层 AI 翻译。

设计哲学（见《NLC规则详解_v3.1.md》）：
    **AI 只翻译，不执行。决策权完全属于用户。**

分层：
    核心层（本包）：分词器 / 风险引擎 / 黑名单 / 词库 —— 纯 Python，零依赖，全部可单测
    接入层        ：云端(OpenAI 兼容) / 本地 / 局域网 / 知识库兜底
    界面层        ：CLI / 图形界面（Windows=tkinter，Linux=GTK3 可选）/ MCP 服务端

平台适配（见 nlc/platform.py）：
    同一套代码同时跑 Linux 与 Windows。平台差异只集中在 platform.py 与 risk_win.py：
        - 配置目录：Windows = %APPDATA%\\nlc ｜ Linux = ~/.config/nlc
        - 风险规则：Linux（bash）与 Windows（cmd / PowerShell）**两套都跑，取最严**
        - 界面    ：Windows = tkinter（Python 自带）｜ Linux 可选用 GTK3
"""

from .risk import ALLOW, BLOCK, CONFIRM, Assessment, assess, explain
from .tokenizer import Token, render, shell_words, summarize, tokenize

__version__ = "0.2.0"

__all__ = [
    "__version__",
    "Token", "tokenize", "shell_words", "render", "summarize",
    "Assessment", "assess", "explain", "ALLOW", "CONFIRM", "BLOCK",
]
