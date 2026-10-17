# -*- coding: utf-8 -*-
"""NLC AI 接入层 —— 主流通用接口（OpenAI 兼容）。

设计（见《NLC规则详解_v3.1.md》第五节）：
    - 协议统一走 **OpenAI 兼容的 /chat/completions** —— 事实上的通用标准，国内主流厂商全支持
    - **零第三方依赖**：只用标准库 urllib。装了 requests 就用 requests（更快），没装也能跑
    - 密钥与主配置**分离存放**（`secrets.json`，权限 600）
    - 支持四条通道：云端 / 本地(Ollama) / 局域网 / 离线知识库兜底
    - 每个通道都有 `test()` 可单独体检

⚠️ 本模块**只负责调用**，不做任何执行动作 —— 见设计原则 1：「AI 只翻译，不执行」。
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .platform import CONFIG_DIR, secure_dir, secure_file

__all__ = ["Provider", "PRESETS", "load_config", "save_config", "set_api_key",
           "get_api_key", "chat", "test", "ChannelError", "CONFIG_DIR", "fallback_chain",
           "CONFIG_PATH", "SECRETS_PATH", "secure_secrets_report",
           "migrate_legacy_config", "LEGACY_CONFIG_DIR"]

#: 配置目录由平台适配层决定：Windows = %APPDATA%\nlc，Linux = ~/.config/nlc
CONFIG_PATH = CONFIG_DIR / "config.json"
SECRETS_PATH = CONFIG_DIR / "secrets.json"


class ChannelError(RuntimeError):
    """通道不可用或调用失败。"""


# --------------------------------------------------------------------------- #
# 预置通道 —— 全是国内可直连的厂商（用户网络访问 Google 系不通）
# --------------------------------------------------------------------------- #
PRESETS: Dict[str, Dict[str, str]] = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "kind": "cloud",
        "note": "中文强、价格低，接口标准。推荐首选",
    },
    "qwen": {
        "name": "通义千问（阿里）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "kind": "cloud",
        "note": "阿里云账号直接开通",
    },
    "kimi": {
        "name": "Kimi（月之暗面）",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "kind": "cloud",
        "note": "长文本见长",
    },
    "glm": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "kind": "cloud",
        "note": "有免费档",
    },
    "siliconflow": {
        "name": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "kind": "cloud",
        "note": "聚合多家开源模型",
    },
    "minimax": {
        "name": "MiniMax",
        "base_url": "https://api.minimax.chat/v1",
        "model": "abab6.5s-chat",
        "kind": "cloud",
        "note": "",
    },
    "stepfun": {
        "name": "阶跃星辰",
        "base_url": "https://api.stepfun.com/v1",
        "model": "step-1-8k",
        "kind": "cloud",
        "note": "",
    },
    "ollama": {
        "name": "本地模型（Ollama）",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:1.5b",
        "kind": "local",
        "note": "完全离线，不需要密钥。本机算力有限，建议小模型",
    },
    "lan": {
        "name": "局域网/电脑上的模型",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "",
        "kind": "lan",
        "note": "指向另一台机器上的模型服务。跨网段可用 adb reverse 打通",
    },
    "custom": {
        "name": "自定义",
        "base_url": "",
        "model": "",
        "kind": "custom",
        "note": "任何 OpenAI 兼容的服务",
    },
}

#: 离线兜底：不调用任何模型，用本地知识库。永远不会失败。
OFFLINE = "offline"


@dataclass
class Provider:
    """一个 AI 接入通道。"""

    id: str
    name: str = ""
    base_url: str = ""
    model: str = ""
    kind: str = "cloud"          # cloud / local / lan / custom
    enabled: bool = True
    timeout: int = 60
    note: str = ""
    key_env: str = ""            # 也允许从环境变量读密钥

    @classmethod
    def from_preset(cls, pid: str, **over: Any) -> "Provider":
        p = dict(PRESETS.get(pid, {"name": pid}))
        p["id"] = pid
        p.setdefault("key_env", "NLC_API_KEY_" + pid.upper())
        p.update({k: v for k, v in over.items() if v is not None})
        return cls(**{k: v for k, v in p.items() if k in cls.__dataclass_fields__})

    @property
    def needs_key(self) -> bool:
        return self.kind in ("cloud", "custom")

    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "active": "auto",                # auto = 按 fallback_chain 依次尝试
    "fallback_chain": ["deepseek", "glm", "qwen", "kimi", "ollama", "lan", OFFLINE],
    "providers": [asdict(Provider.from_preset(k)) for k in
                  ("deepseek", "glm", "qwen", "kimi", "siliconflow", "ollama", "lan", "custom")],
    "ui": {
        "theme": "day",              # day / night
        "translate_enabled": True,   # 个人词库的「翻译开关」
        "danger_full_access": False, # ★ 完全访问模式，默认关，且不允许被设为默认
        #: 判风险与写提示词时按哪种命令行方言。留空 = 用平台默认
        #: （Windows → cmd，Linux → bash）。可选：cmd / powershell / bash
        "shell": "",
    },
}


def _ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    secure_dir(CONFIG_DIR)


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """读配置；不存在则写入一份默认配置。"""
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        _ensure_dir()
        p.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    # 补齐缺失的顶层键，保证升级不炸
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, json.loads(json.dumps(v)))
    return cfg


def save_config(cfg: Dict[str, Any], path: Optional[Path] = None) -> Path:
    p = Path(path) if path else CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _read_secrets() -> Dict[str, str]:
    if not SECRETS_PATH.exists():
        return {}
    try:
        return json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def get_api_key(pid: str, key_env: str = "") -> str:
    """按「环境变量 → 密钥文件」的顺序取密钥。"""
    env_name = key_env or ("NLC_API_KEY_" + pid.upper())
    if os.environ.get(env_name):
        return os.environ[env_name]
    return _read_secrets().get(pid, "")


def set_api_key(pid: str, key: str) -> Path:
    """写入密钥文件，并**尽力收紧到只有本人可读写**。

    ⚠️ 平台差异要如实说明：
        - Linux：`chmod 600` 真的生效。
        - Windows：`os.chmod` 改不了 NTFS 的 ACL，**600 在 Windows 上是空操作**。
          本函数改走 `icacls`（去掉继承、只留当前用户），但仍可能失败。
          想知道到底收紧了没有，调 `secure_secrets_report()`。
    """
    _ensure_dir()
    data = _read_secrets()
    if key:
        data[pid] = key
    else:
        data.pop(pid, None)
    SECRETS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    secure_file(SECRETS_PATH)
    return SECRETS_PATH


def secure_secrets_report() -> List[str]:
    """把「密钥文件到底被收紧了没有」的真实情况报出来（不假装成功）。"""
    if not SECRETS_PATH.exists():
        return ["密钥文件还不存在"]
    return secure_file(SECRETS_PATH)


# --------------------------------------------------------------------------- #
# 从「Linux 版的配置位置」搬过来（只做一次，只复制、不删除）
# --------------------------------------------------------------------------- #
#: Linux 版的配置目录（XDG 约定）。Windows 版用的是 %APPDATA%\nlc。
LEGACY_CONFIG_DIR = Path.home() / ".config" / "nlc"


def migrate_legacy_config() -> List[str]:
    """把老位置（~/.config/nlc）的配置搬到新位置（%APPDATA%\\nlc）。

    为什么需要：同一台机器上从 Linux 版切到 Windows 版时，API Key、个人词库、
    黑名单都躺在老目录里。不搬的话，用户得把 API Key 重新填一遍 ——
    对一个不写代码的人来说这是很烦的事。

    三条安全约束（都不许放宽）：
        1. **只在新的配置目录里完全没有配置时**才搬 —— 绝不覆盖已有配置；
        2. **只复制、不删除** —— 老目录原样留着，随时能退回去；
        3. 密钥文件搬过去后**立刻重新收紧权限**。

    返回一串"做了什么"，供调用方原样展示给用户。
    """
    steps: List[str] = []
    if CONFIG_DIR == LEGACY_CONFIG_DIR:
        return steps                       # Linux 上两者本来就是同一个目录
    if not LEGACY_CONFIG_DIR.exists():
        return steps

    # 约束 1：新位置已经有配置就什么都不做
    existing = [p for p in (CONFIG_PATH, SECRETS_PATH) if p.exists()]
    if existing:
        return steps

    _ensure_dir()
    moved: List[str] = []
    for name in ("config.json", "secrets.json", "prompts.json",
                 "blacklist.json", "wordbank.json"):
        src = LEGACY_CONFIG_DIR / name
        dst = CONFIG_DIR / name
        if not src.exists() or dst.exists():
            continue
        try:
            dst.write_bytes(src.read_bytes())
            moved.append(name)
        except OSError as exc:
            steps.append("搬运 %s 失败：%s" % (name, exc))

    if moved:
        steps.append("已从 %s 搬来：%s" % (LEGACY_CONFIG_DIR, "、".join(moved)))
        steps.append("（只复制、没删除；老目录仍原样保留）")
        if SECRETS_PATH.exists():
            secure_file(SECRETS_PATH)
        steps.append("新配置目录：%s" % CONFIG_DIR)
    return steps


def get_provider(cfg: Dict[str, Any], pid: str) -> Optional[Provider]:
    for d in cfg.get("providers", []):
        if d.get("id") == pid:
            return Provider(**{k: v for k, v in d.items() if k in Provider.__dataclass_fields__})
    if pid in PRESETS:
        return Provider.from_preset(pid)
    return None


def fallback_chain(cfg: Dict[str, Any]) -> List[str]:
    """返回本次要依次尝试的通道顺序，末位永远是离线兜底。"""
    if cfg.get("active") and cfg["active"] != "auto":
        return [cfg["active"], OFFLINE]
    chain = [p for p in cfg.get("fallback_chain", []) if p]
    if OFFLINE not in chain:
        chain.append(OFFLINE)
    return chain


# --------------------------------------------------------------------------- #
# HTTP（零依赖：标准库 urllib；有 requests 就用）
# --------------------------------------------------------------------------- #
def _post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str],
               timeout: int) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    req_headers.update(headers)

    try:
        import requests  # type: ignore
    except ImportError:
        requests = None

    if requests is not None:
        try:
            r = requests.post(url, data=body, headers=req_headers, timeout=timeout)
        except requests.RequestException as exc:
            raise ChannelError("连不上 %s：%s" % (url, exc)) from exc
        if r.status_code >= 400:
            raise ChannelError(_http_hint(r.status_code, r.text[:300]))
        try:
            return r.json()
        except ValueError as exc:
            raise ChannelError("服务返回的不是 JSON：%s" % r.text[:200]) from exc

    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise ChannelError(_http_hint(exc.code, detail)) from exc
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise ChannelError("连不上 %s：%s" % (url, exc)) from exc


def _http_hint(code: int, detail: str) -> str:
    if code in (401, 403):
        return "密钥无效或没有权限（HTTP %d）。请检查 API Key。%s" % (code, detail)
    if code == 404:
        return "地址不对（HTTP 404）。请检查 base_url 是否包含 /v1 之类的前缀。%s" % detail
    if code == 429:
        return "请求太频繁或额度用尽（HTTP 429）。%s" % detail
    if code >= 500:
        return "服务端出错（HTTP %d），稍后再试。%s" % (code, detail)
    return "HTTP %d：%s" % (code, detail)


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def chat(provider: Provider, messages: List[Dict[str, str]],
         temperature: float = 0.2, max_tokens: int = 1024,
         key: str = "", json_mode: bool = False) -> str:
    """调一次对话接口，返回模型输出的文本。

    Args:
        provider: 通道。
        messages: OpenAI 格式的消息列表。
        key: 密钥；留空则按「环境变量 → 密钥文件」自动取。
        json_mode: 请求服务端以 JSON 模式返回（支持的厂商会强约束输出格式，
                   不支持时会被忽略）。

    ★ 思维链处理：推理类模型（如 DeepSeek 的 reasoner）会把思考过程放在
      `reasoning_content` 字段、把正式回答放在 `content` 字段。
      **本函数只取 `content`，直接丢弃 `reasoning_content`** ——
      这是"缩小思维链"最彻底的一步：思考过程根本不会被带进界面。
    """
    api_key = key or get_api_key(provider.id, provider.key_env)
    if provider.needs_key and not api_key:
        raise ChannelError("通道「%s」还没有填 API Key。" % (provider.name or provider.id))
    if not provider.base_url:
        raise ChannelError("通道「%s」还没有填服务地址 base_url。" % (provider.name or provider.id))

    payload: Dict[str, Any] = {
        "model": provider.model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    data = _post_json(provider.endpoint(), payload, headers, provider.timeout)
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ChannelError("返回结构不是预期的 OpenAI 格式：%s" % str(data)[:200]) from exc

    content = (msg.get("content") or "").strip()
    if not content and msg.get("reasoning_content"):
        # 极端情况：模型把东西全放进了思考字段、content 为空。
        # 宁可报错让上层重试，也不把思考过程当答案抛给用户。
        raise ChannelError("模型只返回了思考内容、没有正式回答，请重试一次。")
    return content


def test(provider: Provider, key: str = "") -> Tuple[bool, str]:
    """体检一个通道。返回 (是否可用, 说明)。

    低价做法：让模型回一个固定短词，验证「地址 + 密钥 + 模型名」三件事都通。
    """
    if provider.id == OFFLINE:
        return True, "离线知识库：不需要网络与密钥，永远可用"
    try:
        out = chat(provider, [{"role": "user", "content": "只回复两个字：正常"}],
                   temperature=0, max_tokens=16, key=key)
    except ChannelError as exc:
        return False, str(exc)
    return True, "通了。模型回了：%s" % (out[:40] or "（空）")
