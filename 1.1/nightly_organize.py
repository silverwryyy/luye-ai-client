# -*- coding: utf-8 -*-
"""
夜间整理 · next_todo.md 归拢器
职责：只搬运，不生成。绝不凭空编条目、绝不碰「随笔」段。

三件事：
1. 「待排」里 [YYYY-MM-DD] 到期(<=今天)的条目 -> 挪到「今日」顶部
2. 「今日」里标了 [x] 的 -> 挪到「归档」（带完成日期）
3. 按固定结构重写文件，随笔段原样保留

建议由 Windows 计划任务每晚跑一次。
"""
import re
import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
TODO_FILE = BASE / "next_todo.md"
LOG_FILE = BASE / "organize_log.txt"

SEC_TODAY = "今日"
SEC_QUEUE = "待排"
SEC_DONE = "归档"
SEC_FREE = "随笔"
ALL_SECS = [SEC_TODAY, SEC_QUEUE, SEC_DONE, SEC_FREE]

DATE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\]\s*(.+)$")
DONE_RE = re.compile(r"^\[[xX]\]\s*(.+)$")


def parse_items(text):
    """按 ## 段落把条目拆出来（去符号、去注释、去空行）"""
    sec = {name: [] for name in ALL_SECS}
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            name = s[3:].strip()
            cur = name if name in sec else None
            continue
        if cur is None:
            continue
        if not s or s.startswith("<!--"):
            continue
        sec[cur].append(s.lstrip("-*+ ").strip())
    return sec


def organize(sec, today):
    """返回 (新段落字典, 变更说明列表)"""
    changes = []

    # 1) 待排 -> 到期搬进今日
    due, kept_queue = [], []
    for item in sec[SEC_QUEUE]:
        m = DATE_RE.match(item)
        if m:
            try:
                d = datetime.date.fromisoformat(m.group(1))
            except ValueError:
                kept_queue.append(item)
                continue
            if d <= today:
                due.append(m.group(2).strip())
                changes.append(f"待排 -> 今日：{m.group(2).strip()}")
                continue
        kept_queue.append(item)

    # 2) 今日 -> [x] 的归档
    done, kept_today = [], []
    for item in sec[SEC_TODAY]:
        m = DONE_RE.match(item)
        if m:
            done.append(f"[{today.isoformat()}] {m.group(1).strip()}")
            changes.append(f"今日 -> 归档：{m.group(1).strip()}")
        else:
            kept_today.append(item)

    new = {
        SEC_TODAY: due + kept_today,
        SEC_QUEUE: kept_queue,
        SEC_DONE: done + sec[SEC_DONE],   # 新归档在前
        SEC_FREE: sec[SEC_FREE],
    }
    return new, changes


def render(new):
    lines = ["# 待办", ""]
    lines += [
        f"## {SEC_TODAY}",
        "<!-- 脚本管理：夜间整理时把「待排」里到期的搬到顶部；你可手写追加；做完标 [x]，会自动归档 -->",
    ]
    lines += [f"- {i}" for i in new[SEC_TODAY]]
    lines += ["", f"## {SEC_QUEUE}",
              "<!-- 格式：- [YYYY-MM-DD] 事项；到日子当晚自动挪进「今日」顶部 -->"]
    lines += [f"- {i}" for i in new[SEC_QUEUE]]
    lines += ["", f"## {SEC_DONE}",
              "<!-- 脚本管理：做完的条目自动落到这里，只增不删 -->"]
    lines += [f"- {i}" for i in new[SEC_DONE]]
    lines += ["", f"## {SEC_FREE}",
              "<!-- 你随便写，脚本永不碰这一段 -->"]
    lines += new[SEC_FREE]
    lines.append("")
    return "\n".join(lines)


def main():
    today = datetime.date.today()
    if not TODO_FILE.exists():
        print(f"[{today}] 未找到 {TODO_FILE}，跳过")
        return

    text = TODO_FILE.read_text(encoding="utf-8")
    sec = parse_items(text)
    new, changes = organize(sec, today)
    TODO_FILE.write_text(render(new), encoding="utf-8")

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    if changes:
        body = f"[{stamp}] 整理 {len(changes)} 项：" + "; ".join(changes)
        print(body)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(body + "\n")
    else:
        print(f"[{stamp}] 无变更")


if __name__ == "__main__":
    main()
