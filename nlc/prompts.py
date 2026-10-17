# -*- coding: utf-8 -*-
"""NLC 提示词角色卡（Prompt Profile）。

借鉴 AI Agent 里的「智能体 / 角色卡」：一个档案 = 一张卡 = 一套完整的行为设定。
每张卡决定三件事：
    1. **风格**：说多细、用不用术语、面向谁
    2. **思维链（CoT）**：要不要展示推理过程（默认**彻底关掉**）
    3. **生成参数**：temperature / max_tokens / 是否强制 JSON 模式

★ 本模块针对的核心痛点（用户原话）：
    **「缩小思维链、精准解释、不废话」**

具体做法（不是靠一句"请简洁"，而是靠四层收紧）：
    ┌ 措辞层：列出**明确的禁止项**（不要复述问题、不要总结、不要客套、不要展示推理）
    ├ 结构层：**固定字段的输出契约**（JSON），不给"自由发挥"留空间
    ├ 长度层：每个字段**给出字数上限**（数值约束比形容词有效得多）
    └ 参数层：temperature=0、收紧 max_tokens、开启服务端 JSON 模式

> 关于思维链：DeepSeek 的推理模型会把思考过程放在独立的 `reasoning_content` 字段里。
> NLC **只取 `content`，丢弃 `reasoning_content`** —— 这本身就是最彻底的"缩小思维链"。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .platform import (CONFIG_DIR, SHELL_BASH, SHELL_CMD, SHELL_POWERSHELL,
                       active_shell, shell_hint)

__all__ = ["PromptProfile", "PROFILES", "TASKS", "load_profiles", "save_profiles",
           "get_profile", "build_system_prompt", "PromptError", "base_prompt",
           "task_rule"]

PROFILES_PATH = CONFIG_DIR / "prompts.json"

TASKS = ("translate", "lookup", "explain", "compose")


class PromptError(RuntimeError):
    """角色卡不存在或字段非法。"""


# --------------------------------------------------------------------------- #
# 共用底座：无论哪张卡都成立的部分
# --------------------------------------------------------------------------- #
#: 每种命令行方言各自要额外叮嘱模型的话。
#: ★ 这一段是 Windows 版新加的：不写清楚，模型会给出 Linux 命令，
#:   而用户在 Windows 上根本跑不了 —— 这是"翻译层给错平台"的典型事故。
_SHELL_EXTRA: Dict[str, str] = {
    SHELL_CMD:
        "7. **只写 cmd（命令提示符）能跑的写法**：\n"
        "   - cmd **没有** `ls` / `rm` / `grep` / `cp` / `cat` / `ps` 这些 Linux 命令，\n"
        "     对应的写法是 `dir` / `del` / `findstr` / `copy` / `type` / `tasklist`。\n"
        "   - 顺序执行用 `&`，前一条成功才继续用 `&&`，前一条失败才继续用 `||`。\n"
        "   - 路径用反斜杠；路径里有空格**必须**用双引号包起来。\n"
        "   - 想用管道过滤，配合 `findstr`，不要写 `grep`。",
    SHELL_POWERSHELL:
        "7. **只写 PowerShell 能跑的写法**：\n"
        "   - 优先用 `动词-名词` 形式的原生命令（`Get-ChildItem` / `Remove-Item` /\n"
        "     `Get-Process`），而不是别名的 `ls` / `rm` / `ps` —— 原生命令更清楚。\n"
        "   - 管道里传的是**对象**，不是文本；要过滤用 `Where-Object`，不要用 `findstr`。\n"
        "   - 路径可以用反斜杠，含空格的路径要加引号。\n"
        "   - 破坏性操作 PowerShell **默认会二次询问**，不要加 `-Confirm:$false` 去绕过它。",
    SHELL_BASH:
        "7. **只写 Linux shell 能跑的写法**：不要写 `dir` / `copy` / `del` / `findstr`\n"
        "   这些 Windows 专有命令。",
}


def shell_extra(shell: Optional[str] = None) -> str:
    """取当前方言的补充叮嘱。"""
    return _SHELL_EXTRA.get(shell or active_shell(), _SHELL_EXTRA[SHELL_BASH])


BASE = """你是 NLC（自然语言命令行）的翻译层。你的工作是把人的自然语言，翻译成一条命令行并解释清楚。

## 不可动摇的铁律
1. **你只翻译和解释，你没有执行任何命令的能力**，也不要假装能执行、不要说"我来帮你执行"。
2. **不要复述用户的问题**，不要以"好的""当然""这是一个很好的问题"开头。
3. **不要展示你的思考过程**：不写推理步骤、不写权衡过程、不列备选方案的比较、不写"让我想想"。
   直接给结论。
4. **不要写结尾总结段**，不要写"希望对你有帮助"，不要加免责声明。
5. 解释要写给**完全不懂命令行的人**看；必须用术语时，顺手用一句话解释它。
6. 只输出一个 JSON 对象：不要 Markdown 代码围栏、不要前后缀说明文字。
"""


def base_prompt(shell: Optional[str] = None) -> str:
    """拼出底座提示词（含当前平台的方言叮嘱）。"""
    return BASE + "\n" + shell_extra(shell)


#: 兼容旧名字：默认方言下的底座
BASE_FULL = base_prompt()

# --------------------------------------------------------------------------- #
# 三种风格：只改「说多细」，不改铁律
# --------------------------------------------------------------------------- #
STYLE_RULES: Dict[str, str] = {
    "concise": """## 风格：极简（当前档案）
- summary **不超过 30 个汉字**，只说这条命令做什么，不要展开。
- 每个词块解释 **不超过 25 个汉字**。抓最要紧的一点说，不要面面俱到。
- notes **不超过 40 个汉字**；没有真正重要的提醒就留空字符串。
- 宁少勿多。信息密度优先，宁可信息少一点，也不要凑字数。""",

    "balanced": """## 风格：平衡（当前档案）
- summary **不超过 50 个汉字**，说清这条命令做什么。
- 每个词块解释 **不超过 45 个汉字**，讲清它在这里起什么作用。
- notes **不超过 80 个汉字**，可以给一个更稳妥的替代写法。
- 在"够用"和"不啰嗦"之间取平衡。""",

    "teacher": """## 风格：讲解（当前档案，面向完全的新手）
- summary **不超过 60 个汉字**，用生活化的比方讲清这条命令在干什么。
- 每个词块解释 **不超过 70 个汉字**。符号类词块（`|` `*` `>` `-`）要重点讲人话，
  因为它们对新手最陌生。
- notes **不超过 120 个汉字**，可以补充"什么情况下会用到""常见坑"。
- 允许适度展开，但**依然禁止**展示思考过程和写总结段。""",

    "expert": """## 风格：老手（当前档案，读者熟练使用命令行）
- summary **不超过 25 个汉字**，直接说目的。
- 每个词块解释 **不超过 15 个汉字**，只讲非显而易见的部分；一眼就懂的直接写"常规"两个字。
- notes **不超过 50 个汉字**，只写坑和替代方案。
- 不要解释基础概念，不要打比方。""",
}

# --------------------------------------------------------------------------- #
# 任务契约：输出格式（每个任务固定，不给自由发挥的空间）
# --------------------------------------------------------------------------- #
#: 每种方言里「一个典型命令词块」长什么样 —— 用来给模型举例子。
#: 举错例子比不举例更糟（模型会照着例子学错平台），所以这里按方言分开写。
_SHELL_EXAMPLES: Dict[str, str] = {
    SHELL_CMD: "`dir`、`findstr`、`|`",
    SHELL_POWERSHELL: "`Get-ChildItem`、`Where-Object`、`|`",
    SHELL_BASH: "`du`、`grep`、`|`",
}

#: 「选项参数会被怎么切分」的示例 —— 各平台写法不同，示例必须跟着变
_SHELL_FLAG_DEMO: Dict[str, str] = {
    SHELL_CMD: "`/s` 会切成 `/` 和 `s` 两个词块",
    SHELL_POWERSHELL: "`-Recurse` 会切成 `-` 和 `Recurse` 两个词块",
    SHELL_BASH: "`-sh` 会切成 `-` 和 `sh` 两个词块",
}

TASK_RULES: Dict[str, str] = {
    "translate": """## 本次任务：把一句话翻译成一条命令
输出 JSON，只有这三个字段：
{"command": "一条可直接执行的命令", "summary": "这条命令做什么", "notes": "提醒或更稳妥的替代；没有就给空字符串"}

要求：
- `command` 只放命令本身，不要加提示符、不要加注释、不要换行。
- **必须是 {shell}**，不要混入其它平台的写法。
- 优先给**常见、稳妥、可读**的写法。不要炫技，不要用生僻参数。
- 如果用户的需求有多种合理做法，只给最常用的一种，把其它做法放进 `notes`。""",

    "lookup": """## 本次任务：查词 —— 一个词对应一个命令词块
先明确概念：**命令词块 = 一个英文单词，或一个符号**（如 {examples}）。
输出 JSON，只有这三个字段：
{"command": "单个命令词块", "summary": "它是什么", "notes": "更详细的说明、常见搭配或近亲命令；没有就给空字符串"}

要求：
- `command` **必须只有一个命令词块**。若用户给的是一个"整件事"（如"压缩文件夹"），
  给出这件事最核心的那个词块，不要给一整条命令。
- `command` 必须是 {shell} 里真实存在的词块。
- `notes` 可以提它的近亲命令，帮用户对照着理解。""",

    "explain": """## 本次任务：逐词块解释一条现成命令
用户会给你一条命令，以及它被切分好的词块列表（带下标）。
输出 JSON，只有一个字段：
{"items": [{"i": 0, "meaning": "这个词块在这里的作用"}, {"i": 1, "meaning": "…"}]}

要求：
- **必须为每一个下标都给出一条解释**，一个不漏、一个不多。下标的数量和顺序必须与输入一致。
- 注意切分方式：{flag_demo}，请**分别**说明各自的作用。
- 重复出现的词块（例如两个 `|`）要按下标分别说明它在**各自位置**上干什么。
- 不要重复整条命令的含义，只说这个词块自己的作用。
- 解释时按 {shell} 的语义讲，不要套用别的平台。""",

    "compose": """## 本次任务：把若干条命令拼成一个脚本
输出 JSON，只有这三个字段：
{"command": "组合后的脚本，含换行", "summary": "这个脚本做什么", "notes": "风险提醒；没有就给空字符串"}

要求：
- **不要要求逻辑连贯**。用户可能就是想把这几个互不相干的命令放在一起跑，这是允许的。
- **不要自作主张删改或"优化"**用户的命令。
- 每条命令之间插入一行分隔输出：`echo "──────"`，便于区分是哪个命令的输出。
- 不要添加用户没要求的东西（不要加错误处理、不要加判断）。
- 若某条命令有风险，就在 `notes` 里提醒，但**不要擅自改掉它** —— 改不改由用户决定。""",
}


def task_rule(task: str, shell: Optional[str] = None) -> str:
    """取任务契约，并按当前方言把示例与措辞填进去。"""
    sk = shell or active_shell()
    return (TASK_RULES[task]
            .replace("{shell}", shell_hint(sk))
            .replace("{examples}", _SHELL_EXAMPLES.get(sk, _SHELL_EXAMPLES[SHELL_BASH]))
            .replace("{flag_demo}", _SHELL_FLAG_DEMO.get(sk, _SHELL_FLAG_DEMO[SHELL_BASH])))


# --------------------------------------------------------------------------- #
# 角色卡
# --------------------------------------------------------------------------- #
@dataclass
class PromptProfile:
    """一张提示词角色卡。"""

    id: str
    name: str = ""            # 卡片名字，如「极简模式」
    desc: str = ""            # 一句话简介，显示在卡片上
    emoji: str = ""           # 卡片头像
    style: str = "concise"    # concise / balanced / teacher / expert
    cot: str = "none"         # none / brief —— 思维链展示程度（默认彻底关）
    temperature: float = 0.0
    max_tokens: int = 900
    json_mode: bool = True    # 要求服务端以 JSON 模式返回（支持时生效）
    builtin: bool = True
    overrides: Dict[str, str] = field(default_factory=dict)   # 按任务覆盖整段提示词

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PromptProfile":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


#: 内置的四张卡。用户复制其中一张改成自己的，就是自定义卡。
PROFILES: Dict[str, PromptProfile] = {
    "concise": PromptProfile(
        id="concise", name="极简模式", desc="最短输出、零废话。日常首选", emoji="⚡",
        style="concise", cot="none", temperature=0.0, max_tokens=700),
    "balanced": PromptProfile(
        id="balanced", name="平衡模式", desc="信息量和篇幅取中间值", emoji="⚖",
        style="balanced", cot="none", temperature=0.0, max_tokens=900),
    "teacher": PromptProfile(
        id="teacher", name="讲解模式", desc="面向完全的新手，多打比方、多讲符号", emoji="🎓",
        style="teacher", cot="none", temperature=0.2, max_tokens=1400),
    "expert": PromptProfile(
        id="expert", name="老手模式", desc="只讲非显而易见的部分，一眼懂的不解释", emoji="🔧",
        style="expert", cot="none", temperature=0.0, max_tokens=600),
}


def load_profiles(path: Optional[Path] = None) -> Dict[str, PromptProfile]:
    """读角色卡：内置四张 + 用户自定义的（用户的覆盖同 id 的内置卡）。"""
    out = {k: PromptProfile.from_dict(v.to_dict()) for k, v in PROFILES.items()}
    p = Path(path) if path else PROFILES_PATH
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for d in data.get("profiles", []):
                prof = PromptProfile.from_dict(d)
                prof.builtin = False
                out[prof.id] = prof
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return out


def save_profiles(profiles: Dict[str, PromptProfile], path: Optional[Path] = None) -> Path:
    p = Path(path) if path else PROFILES_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": 1,
            "profiles": [v.to_dict() for v in profiles.values() if not v.builtin]}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def get_profile(pid: str = "concise", path: Optional[Path] = None) -> PromptProfile:
    profs = load_profiles(path)
    if pid not in profs:
        raise PromptError("没有这张角色卡：%s（现有：%s）" % (pid, "、".join(profs)))
    return profs[pid]


def build_system_prompt(profile: PromptProfile, task: str) -> str:
    """把「底座 + 风格 + 任务契约」拼成最终的系统提示词。

    Args:
        profile: 角色卡。
        task: translate / lookup / explain / compose。

    ★ 平台差异在这里落地：底座与任务契约都会带上当前命令行方言
      （cmd / PowerShell / bash）。不这么做，模型会给出另一个平台的命令。
    """
    if task not in TASKS:
        raise PromptError("未知任务：%s" % task)

    # 任务级的整段覆盖（自定义卡可以完全替换某个任务的提示词）
    if task in profile.overrides and profile.overrides[task].strip():
        return profile.overrides[task].strip()

    parts = [base_prompt()]

    if profile.cot == "none":
        parts.append("## 思维链\n本轮**禁止**输出任何推理过程、思考步骤或中间推导。直接给最终结果。")
    elif profile.cot == "brief":
        parts.append("## 思维链\n最多用一句话交代判断依据，不要展开推理过程。")

    parts.append(STYLE_RULES.get(profile.style, STYLE_RULES["concise"]))
    parts.append(task_rule(task))
    return "\n\n".join(parts)


def profile_summary() -> str:
    """给命令行看的一行卡片列表。"""
    out = []
    for pid, p in load_profiles().items():
        flag = "内置" if p.builtin else "自定义"
        out.append("  %-10s %-2s %-8s %-4s temp=%.1f  %s"
                   % (pid, p.emoji, p.name, flag, p.temperature, p.desc))
    return "\n".join(out)
