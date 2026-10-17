# -*- coding: utf-8 -*-
"""MCP 服务端 stdio 端到端测试。

和 `nlc mcp --selftest` 的区别：那个只调 handle_message() 函数；
**这个是真的把服务端当子进程起起来、走 stdin/stdout 管道收发**，
所以它会验证到真实传输层的问题（刷缓冲、stdout 被日志污染、进程不退出等）。

跑法：python3 tests/mcp_stdio_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 英文 Windows 的控制台默认 cp1252，打印中文会 UnicodeEncodeError —— 统一强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  ok   %-46s %s" % (label, got))
    else:
        FAIL += 1
        print("  FAIL %-46s 得到 %r，期望 %r" % (label, got, want))


def main() -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        [sys.executable, "-m", "nlc.mcp_server"],
        cwd=ROOT, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1)

    def send(obj) -> None:
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def recv():
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("服务端没有回应就退出了")
        return json.loads(line)

    print("=== 一、MCP 生命周期握手 ===")
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05",
                     "capabilities": {},
                     "clientInfo": {"name": "stdio-test", "version": "0"}}})
    r = recv()
    check("initialize 的 id 回显正确", r["id"], 1)
    check("返回了 protocolVersion", "protocolVersion" in r["result"], True)
    check("声明了 tools 能力", "tools" in r["result"]["capabilities"], True)
    check("服务器自称 nlc", r["result"]["serverInfo"]["name"], "nlc")
    print("      instructions: %s" % r["result"]["instructions"].splitlines()[0][:60])

    # 通知：不该有任何回复
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    r = recv()
    check("通知不回复（下一个收到的是 ping 的回复）", r["id"], 2)

    print("\n=== 二、tools/list ===")
    send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    r = recv()
    tools = r["result"]["tools"]
    names = [t["name"] for t in tools]
    check("工具数量", len(tools), 6)
    check("含风险判定工具", "nlc_assess_risk" in names, True)
    check("每个工具都带 inputSchema",
          all("inputSchema" in t and "name" in t and "description" in t for t in tools), True)
    bad = [n for n in names if any(k in n.lower() for k in ("exec", "run", "shell", "eval"))]
    check("★ 没有任何执行类工具", bad, [])

    print("\n=== 三、tools/call：不需要密钥的那两个 ===")
    for mid, tool, args, want_key in [
        (4, "nlc_assess_risk", {"command": "rm -rf /usr"}, "block"),
        (5, "nlc_assess_risk", {"command": "ls && rm -rf /tmp/x"}, "confirm"),
        (6, "nlc_assess_risk", {"command": "env rm -rf /"}, "block"),
        (7, "nlc_assess_risk", {"command": "du -sh *"}, "allow"),
    ]:
        send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
              "params": {"name": tool, "arguments": args}})
        r = recv()
        body = json.loads(r["result"]["content"][0]["text"])
        check("assess(%s)" % args["command"], body["level"], want_key)

    send({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
          "params": {"name": "nlc_tokenize",
                     "arguments": {"command": 'find . -name "*.log" -delete'}}})
    r = recv()
    body = json.loads(r["result"]["content"][0]["text"])
    # find . -name "*.log" -delete
    # → find / . / - / name / " / * / . / log / " / - / delete  = 11 个词块
    check("tokenize 词块数", body["count"], 11)
    check("tokenize 首个词块是 find", body["tokens"][0]["text"], "find")
    check("★ -name 被切成 - 和 name",
          [t["text"] for t in body["tokens"][2:4]], ["-", "name"])

    print("\n=== 四、错误处理 ===")
    send({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
          "params": {"name": "不存在的工具", "arguments": {}}})
    r = recv()
    body = json.loads(r["result"]["content"][0]["text"])
    check("未知工具给出可用列表", "error" in body and "nlc_assess_risk" in body["error"], True)

    send({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
          "params": {"name": "nlc_assess_risk", "arguments": {}}})
    r = recv()
    body = json.loads(r["result"]["content"][0]["text"])
    check("缺参数时明确报错而不是崩", "error" in body, True)

    send({"jsonrpc": "2.0", "id": 11, "method": "不存在的方法"})
    r = recv()
    check("未知方法返回 JSON-RPC 错误", r.get("error", {}).get("code"), -32601)

    print("\n=== 五、真实传输层检查 ===")
    send({"jsonrpc": "2.0", "id": 12, "method": "ping"})
    r = recv()
    check("管道仍然畅通（第 5 轮往返）", r["id"], 12)

    # 关掉 stdin → 服务端应当自己退出
    proc.stdin.close()
    try:
        code = proc.wait(timeout=10)
        check("stdin 关闭后服务端自行退出", code, 0)
    except subprocess.TimeoutExpired:
        proc.kill()
        check("stdin 关闭后服务端自行退出", "超时未退出", 0)

    err = proc.stderr.read()
    noise = [l for l in err.splitlines() if l and not l.startswith("[nlc] ")]
    check("stderr 里只有带 [nlc] 前缀的日志（无杂音）", noise, [])
    print("      服务端 stderr 共 %d 行（都带 [nlc] 前缀）" % len(err.strip().splitlines()))

    print("\n" + "=" * 62)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())
