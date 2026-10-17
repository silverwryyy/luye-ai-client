# -*- coding: utf-8 -*-
"""
开机问候 · 单发脚本
- 触发权在启动器（快捷方式/BAT/VBS），脚本自己不做"要不要问候"的判断
- 同一天最多 MAX_PER_DAY 次，跨天自动清零，防调试重启刷屏
- 只发一轮请求，拿回一句问候就结束
- 开机可能没网：先探 443，1 秒定生死；通了才调模型，不通直接走本地模板
- 旧账来源：next_todo.md 的「今日」段（夜间由 nightly_organize.py 归拢）
"""
import os
import json
import socket
import datetime
from urllib import request as urlrequest
from pathlib import Path

BASE = Path(__file__).resolve().parent
TODO_FILE = BASE / "next_todo.md"
STATE_FILE = BASE / "last_greeting.json"
OUT_FILE = BASE / "greeting_output.txt"

MAX_PER_DAY = 5          # 同一天最多问候几次
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"
TIMEOUT = 8              # 模型请求超时：探测已确认有网，8 秒足够，不再硬耗 30 秒
PROBE_HOST = "api.deepseek.com"
PROBE_PORT = 443
PROBE_TIMEOUT = 1.0      # 开机探测：1 秒内给结论，没 WiFi 时气泡照样秒出

SEC_TODAY = "今日"


def load_state():
    today = datetime.date.today().isoformat()
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    if state.get("date") != today:
        state = {"date": today, "count": 0}
    return state


def save_state(state):
    try:
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def time_slot(now):
    h = now.hour
    if 6 <= h < 11:
        return "早上"
    if 11 <= h < 14:
        return "中午"
    if 14 <= h < 18:
        return "下午"
    if 18 <= h < 23:
        return "晚上"
    return "深夜"


def read_todos(n=3):
    """只读「## 今日」段的前 n 条，当开机问候的旧账"""
    if not TODO_FILE.exists():
        return []
    items = []
    in_today = False
    for line in TODO_FILE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("## "):
            in_today = (SEC_TODAY in s[3:].strip())
            continue
        if not in_today:
            continue
        if not s or s.startswith("<!--"):
            continue
        items.append(s.lstrip("-*+ ").strip())
        if len(items) >= n:
            break
    return items


def build_prompt(now):
    todos = read_todos()
    todo_txt = "\n".join(f"- {t}" for t in todos) if todos else "- （今天没有待办）"
    return (
        "[开机问候]\n"
        f"现在是 {now:%Y-%m-%d %H:%M}（{time_slot(now)}）。\n"
        f"待办前三件：\n{todo_txt}\n\n"
        "用两句话以内打个招呼，开头用「老大」称呼对方，只喊一次；"
        "只挑一件最紧的旧账提一句，"
        "不列清单、不展开对话，说完立刻结束。语气贴合当前时段。"
    )


def net_ok():
    """开机可能还没连上网：1 秒内探一次 api 端口。
    通 → 允许调模型；不通 → 立刻短路走本地模板，不让气泡迟到半分钟。"""
    try:
        with socket.create_connection((PROBE_HOST, PROBE_PORT), PROBE_TIMEOUT):
            return True
    except Exception:
        return False


def call_model(prompt):
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode("utf-8")
    req = urlrequest.Request(
        API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def fallback(now):
    todos = read_todos(1)
    tail = f"，先看：{todos[0]}" if todos else ""
    return f"{time_slot(now)}好{tail}。"


def build_greeting():
    """生成一句问候并返回；受每日次数限制，超限返回 None。
    纯生成：不打印、不落盘，交给调用方决定怎么用。"""
    state = load_state()
    if state["count"] >= MAX_PER_DAY:
        return None  # 今天说够了，静默退出
    state["count"] += 1
    save_state(state)

    now = datetime.datetime.now()
    # 没网就用模板（秒回），有网才请模型润色；模型失手也还有模板兜底
    if net_ok():
        msg = call_model(build_prompt(now))
        if msg:
            return msg
    return fallback(now)


def main():
    msg = build_greeting()
    if not msg:
        return
    print(msg)
    try:
        OUT_FILE.write_text(msg, encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    main()
