# -*- coding: utf-8 -*-
"""memory.py 回归自测（Luye v1.2）。

跑法：python _selftest_memory.py
全程在临时目录操作，**绝不碰真实的 history.json**。
改完 memory.py / 记忆接线后必跑（见 AGENTS.md「改记忆机制时的规矩」）。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memory

tmp = tempfile.mkdtemp(prefix="memtest_")
memory.HISTORY_FILE = os.path.join(tmp, "history.json")
memory.BACKUP_DIR = os.path.join(tmp, "_backup")
memory.LONG_TERM_FILE = os.path.join(tmp, "long_term.json")
memory.ANCHORS_FILE = os.path.join(tmp, "core_anchors.md")
memory._STATE = None

# ---- 1) v1.0 裸 list 自动升 v2，噪声剥掉，原文备份 ----
legacy = [
    {"role": "system", "content": "旧的系统提示词"},
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "[📄 路夜正在读取文件: x]\n正文内容"},
]
with open(memory.HISTORY_FILE, "w", encoding="utf-8") as f:
    json.dump(legacy, f, ensure_ascii=False)

st = memory.load_state(force=True)
assert st["schema_version"] == 2, st["schema_version"]
assert st["legacy_system"] == "旧的系统提示词", st["legacy_system"]
assert st["system"] == memory.DEFAULT_SYSTEM_PROMPT
assert len(st["recent"]) == 2, len(st["recent"])
assert "[📄" not in st["recent"][1]["content"], st["recent"][1]["content"]
assert os.path.isdir(memory.BACKUP_DIR) and os.listdir(memory.BACKUP_DIR), "no legacy backup"

# ---- 2) 保存走原子写，不留 .tmp ----
msgs = memory.to_messages(st)
msgs.append({"role": "user", "content": "第二问"})
memory.save_messages(msgs)
st2 = memory.load_state(force=True)
assert st2["recent"][-1]["content"] == "第二问", st2["recent"][-1]
assert not os.path.exists(memory.HISTORY_FILE + ".tmp"), ".tmp leaked"
with open(memory.HISTORY_FILE, encoding="utf-8") as f:
    assert json.load(f)["schema_version"] == 2

# ---- 3) 折叠：只吃 L3，摘要唯一，原文进 archive ----
for i in range(40):
    st2["recent"].append({"role": "user", "content": "问题%d" % i, "tier": "L3"})
total = len(st2["recent"])
assert memory.fold(st2, lambda p: "摘要A", 20, 8)
assert st2["rolling_summary"] == "摘要A"
assert len(st2["recent"]) == 8, len(st2["recent"])
assert len(st2["archive"]) == total - 8, len(st2["archive"])

# ---- 3b) 再折一次：旧摘要并入后覆盖，绝不叠出第二条 ----
for i in range(40):
    st2["recent"].append({"role": "user", "content": "追问%d" % i, "tier": "L3"})


def _fake2(payload):
    assert "【已有摘要】摘要A" in payload, "old summary not carried in"
    return "摘要B"


assert memory.fold(st2, _fake2, 20, 8)
assert st2["rolling_summary"] == "摘要B"
assert len(st2["recent"]) == 8
msgs2 = memory.to_messages(st2)
assert sum(1 for m in msgs2 if "滚动摘要" in (m.get("content") or "")) == 1, "summary block duplicated"

# ---- 3c) 结构：人格 -> 滚动摘要 -> 对话；L1 锚点由 desktop_bug 插在第 1 位（摘要之前）----
assert msgs2[0]["content"] == memory.DEFAULT_SYSTEM_PROMPT
assert "滚动摘要" in msgs2[1]["content"], msgs2[1]["content"][:40]
assert msgs2[2]["role"] in ("user", "assistant")
layered = list(msgs2)
layered.insert(1, {"role": "system", "content": memory.build_anchors_prompt()})
assert "核心锚点" in layered[1]["content"], "L1 must sit before summary"
assert "滚动摘要" in layered[2]["content"], "summary must follow anchors"

# ---- 4) L1 可被 core_anchors.md 覆盖 ----
with open(memory.ANCHORS_FILE, "w", encoding="utf-8") as f:
    f.write("自定义锚点内容")
assert memory.load_core_anchors() == "自定义锚点内容"
os.remove(memory.ANCHORS_FILE)
assert "唯一" in memory.load_core_anchors() and "服务对象" in memory.load_core_anchors()

# ---- 5) L2 命中才注入 ----
memory.register_topic("Luye开发", ["打包", "开源"], "v1.2 三层记忆已落地")
assert "Luye开发" in memory.build_long_term_prompt("我们聊聊打包的事")
assert memory.build_long_term_prompt("今天天气不错") == ""

# ---- 6) 坏文件不崩，退回默认 ----
with open(memory.HISTORY_FILE, "w", encoding="utf-8") as f:
    f.write("{ this is not json")
bad = memory.load_state(force=True)
assert bad["schema_version"] == 2 and bad["recent"] == []

# ---- 7) desktop_bug.py 的接线确实在位 ----
src_path = os.path.join(HERE, "desktop_bug.py")
with open(src_path, "r", encoding="utf-8") as f:
    src = f.read()
assert 'temp_messages.insert(1, {"role": "system", "content": _anchors})' in src, "anchors injection missing"
assert "memory.build_long_term_prompt(user_input)" in src, "L2 injection missing"
assert "memory.fold(state, _summarize" in src, "fold not wired"
assert "memory.save_messages(messages)" in src, "atomic save not wired"

sys.stdout.write("SELFTEST_OK\n")
