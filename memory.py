# -*- coding: utf-8 -*-
"""memory.py —— 路夜三层记忆引擎（Luye v1.2）

分层定义（详见 AGENTS.md「分层记忆与选择性遗忘」）：
  L1 核心锚点   永不压缩，每轮强制注入（代码兜底 + core_anchors.md 可覆盖）
  L2 长期议题   只增不删，命中 tags 才注入（long_term.json，v1.2 手动登记）
  L3 临时上下文  近期对话，超限才折叠；折叠只吃 L3，原文进 archive 不销毁

history.json v3 结构（一轮一行：user 与 assistant 绑在同一个对象里，
孤儿回复从结构上不可能产生；主动性消息单开 proactive，不冒充对话）：
{
  "schema_version": 3,
  "system":        "<人格提示词>",
  "rolling_summary": "<唯一一条滚动摘要，合并式覆盖，绝不层层套娃>",
  "recent":    [{"t","day","user","assistant"}...],   # 一轮一行
  "proactive": [{"t","day","text"}...],               # 路夜主动说的话
  "archive":   [{"ts","role","content","reason"}...],
  "legacy_system": "<升级前的人格提示词，仅留痕>",
  "saved_at": "YYYY-mm-dd HH:MM:SS"
}
向后兼容：裸 list（v1.0）→ v3；v2 扁平 recent → v3 按轮配对。原文先备份到
_backup\\history_v<旧版本>_<时间戳>.json，旧内容一条不丢。
"""

import datetime
import json
import os
import re
import shutil

SCHEMA_VERSION = 3

ROOT = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(ROOT, "history.json")
ANCHORS_FILE = os.path.join(ROOT, "core_anchors.md")
LONG_TERM_FILE = os.path.join(ROOT, "long_term.json")
BACKUP_DIR = os.path.join(ROOT, "_backup")

MAX_HISTORY_MESSAGES = 20     # 与 desktop_bug.py 保持一致（对外口径：消息条数）
KEEP_RECENT_MESSAGES = 8      # 折叠后保留的消息条数（折半即轮数）
SUMMARY_MAX_CHARS = 1500
ARCHIVE_MAX_ITEMS = 300
ARCHIVE_SPILL_FILE = os.path.join(BACKUP_DIR, "archive_spill.jsonl")  # 溢出条目落地，真可捞回
L2_INJECT_MAX = 3
PROACTIVE_MAX_ITEMS = 200

DEFAULT_SYSTEM_PROMPT = (
    "你是老大的赛博助手，名叫路夜。拥有联网搜索、扫描本地目录、读取本地文件、写入本地文件的能力。"
    "回答要简短精干。当你需要执行操作时，必须调用工具，不要只用文字描述你要做什么。"
    "**调用工具时，请严格按照工具定义发送工具调用请求，不要将工具调用写成文本。** "
    "当你读取代码文件时，如果文件较长，请分块读取。**如果你调用 read_file 发现提示有后续内容，"
    "你必须立即再次调用 read_file 继续读，直到读完为止，才能给出结论。** "
    "当你需要写文件时，必须先告诉老大你要写的路径和内容，得到确认后再调用工具。"
    "严禁写入或覆盖系统盘的核心文件！"
)

# ---------------- L1 核心锚点（兜底版；data 侧 core_anchors.md 存在时以文件为准） ----------------
CORE_ANCHORS_FALLBACK = """## 身份
- 你是该用户唯一的服务对象，必须称呼他「老大」。
- 专业背景：随对话逐步补充，以用户自己说明为准。

## 交互底线
- 危险命令必须带 why（为什么跑、要什么结果），没写就当打回、不盲批。
- 写文件前先说清路径与内容；严禁写系统盘核心文件。
- 回答简短精干，每句话以「喵」结尾。

## 长期铁律（摘自 AGENTS.md）
- 长任务走 tools\\run_bg.py，别用 run_command 硬扛（L1）。
- 依赖的二进制 / 包装只放本地固定盘 C:/D:，绝不依赖可移动盘（L3）。
"""

# L3 噪声行：工具提示、进度提示这类「过程噪声」，落盘前剥掉，这就是优雅遗忘的第一层。
_NOISE_LINE = re.compile(r"^\s*\[(?:🛰️|📄|📂|🖥️|📝|🧠|✅|⚠️|❌|🔎|📖)[^\]]*\]\s*$")
_SUMMARY_PREFIX = "【L3 滚动摘要·早期对话已压缩】"

_STATE = None


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _day_of(ts):
    ts = str(ts or "")
    if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-":
        return ts[:10]
    return datetime.date.today().isoformat()


def _summary_body(text):
    """去掉摘要注入前缀，拿回正文。历史里混进 recent 的 system 摘要行靠它救回。"""
    t = (text or "").strip()
    if t.startswith(_SUMMARY_PREFIX):
        t = t[len(_SUMMARY_PREFIX):].strip()
    return t


# ==================== 落盘：原子写 ====================
def atomic_write_json(path, obj):
    """先写 .tmp 再 os.replace，断电/崩溃也不会留下半个文件。"""
    d = os.path.dirname(path) or "."
    if not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _spill_archive(items):
    """archive 超出上限的旧条目追加进 jsonl（只增不删）。返回写入条数，失败返回 0 不阻断折叠。"""
    if not items:
        return 0
    try:
        d = os.path.dirname(ARCHIVE_SPILL_FILE)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        with open(ARCHIVE_SPILL_FILE, "a", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        return len(items)
    except Exception:
        return 0


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _backup_legacy(path, tag="v1"):
    """升级前把旧文件原样备份一份，防手滑。返回备份路径或空串。"""
    try:
        if not os.path.exists(BACKUP_DIR):
            os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.splitext(os.path.basename(path))[0]
        dst = os.path.join(BACKUP_DIR, "%s_%s_%s.json" % (base, tag, stamp))
        if not os.path.exists(dst):
            shutil.copy2(path, dst)
        return dst
    except Exception:
        return ""


# ==================== L3 清洗（优雅遗忘的第一层：剥噪声） ====================
def sanitize_text(text):
    """剥掉工具提示行 / 压掉行尾空白。只清过程噪声，不动正文。"""
    if not text:
        return ""
    keep = []
    for ln in str(text).splitlines():
        if _NOISE_LINE.match(ln):
            continue
        keep.append(ln.rstrip())
    return "\n".join(keep).strip()


# ==================== 轮 ⇄ 消息 互转（v3 的骨架） ====================
def _messages_to_turns(msgs):
    """扁平 messages → (turns, proactive, summary_bodies)。

    规则：
      user      开新轮
      assistant 补进「当前这轮的 assistant」（只在为空时补位）
      接不上任何轮的 assistant → 进 proactive（主动性消息，不冒充对话）
      system    → 摘要候选（调用方负责过滤掉人格提示词）
    """
    turns, proactive, sums = [], [], []
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if not content:
            continue
        content = str(content)
        ts = m.get("ts") or m.get("t") or _now()
        if role == "system":
            body = _summary_body(content)
            if body:
                sums.append(body)
            continue
        txt = sanitize_text(content)
        if not txt:
            continue
        if role == "user":
            turns.append({"t": str(ts), "day": _day_of(ts), "user": txt, "assistant": ""})
        elif role == "assistant":
            if turns and not turns[-1]["assistant"]:
                turns[-1]["assistant"] = txt
            else:
                proactive.append({"t": str(ts), "day": _day_of(ts), "text": txt})
    return turns, proactive, sums


def _normalize_turn(r):
    if not isinstance(r, dict):
        return None
    if "role" in r:                      # 老扁平格式混进来时兜底
        return None
    u = sanitize_text(r.get("user") or "")
    a = sanitize_text(r.get("assistant") or "")
    if not u and not a:
        return None
    ts = r.get("t") or r.get("ts") or _now()
    return {"t": str(ts), "day": str(r.get("day") or _day_of(ts)),
            "user": u, "assistant": a}


def _normalize_proactive(p):
    if not isinstance(p, dict):
        return None
    txt = sanitize_text(p.get("text") or "")
    if not txt:
        return None
    ts = p.get("t") or p.get("ts") or _now()
    return {"t": str(ts), "day": str(p.get("day") or _day_of(ts)), "text": txt}


def _turn_msg_count(r):
    """一轮折算成几条消息：有 assistant 算 2 条，半轮算 1 条。
    fold / needs_fold 全靠它把「轮」换算回主程序的「消息条数」口径。"""
    return 2 if (r.get("assistant") or "").strip() else 1


def _total_msg_count(recent):
    return sum(_turn_msg_count(r) for r in (recent or []) if isinstance(r, dict))


def _iter_flat(state):
    """state → 严格交替的 [{"role","content"}]。空 assistant 不产出消息。"""
    out = []
    for r in state.get("recent") or []:
        if not isinstance(r, dict):
            continue
        if "role" in r:                       # 老扁平格式兜底
            role, content = r.get("role"), r.get("content")
            if role in ("user", "assistant") and content:
                txt = str(content).strip()
                if out and out[-1]["role"] == role:
                    out[-1]["content"] = (out[-1]["content"] + "\n\n" + txt).strip()
                else:
                    out.append({"role": role, "content": txt})
            continue
        u = (r.get("user") or "").strip()
        a = (r.get("assistant") or "").strip()
        if u:
            out.append({"role": "user", "content": u})
        if a:
            if out and out[-1]["role"] == "assistant":
                out[-1]["content"] = (out[-1]["content"] + "\n\n" + a).strip()
            else:
                out.append({"role": "assistant", "content": a})
    return out


# ==================== schema 迁移：v1 / v2 → v3 ====================
def default_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "system": DEFAULT_SYSTEM_PROMPT,
        "rolling_summary": "",
        "recent": [],
        "proactive": [],
        "archive": [],
        "legacy_system": "",
        "saved_at": _now(),
    }


def migrate(raw, path=""):
    """把任意旧结构升成 v3。缺字段补默认值，旧内容一条不丢。"""
    st = default_state()

    # ---- v1.0 裸 list ----
    if isinstance(raw, list):
        if path and not os.path.exists(path):
            pass
        if path:
            _backup_legacy(path, "v1")
        turns, proa, sums = _messages_to_turns(raw)
        st["recent"], st["proactive"] = turns, proa
        if sums:
            st["rolling_summary"] = sums[-1][:SUMMARY_MAX_CHARS]
        return st

    if not isinstance(raw, dict):
        return st

    ver = raw.get("schema_version")
    if not isinstance(ver, int):
        ver = 1
    if path and ver != SCHEMA_VERSION:
        _backup_legacy(path, "v%d" % ver)

    # ---- 人格提示词 / 旧人格留痕 ----
    # v2 起人格已由代码版接管，v2 文件里的 system 就是正式人格，必须继承；
    # 只有 v1.x 的 system 才算「上一版人格」，留痕进 legacy_system。
    sys_raw = raw.get("system")
    if isinstance(sys_raw, str) and sys_raw.strip():
        if ver >= 2:
            st["system"] = sys_raw.strip()
        else:
            st["legacy_system"] = sys_raw.strip()
    elif isinstance(sys_raw, dict) and sys_raw.get("content"):
        st["legacy_system"] = str(sys_raw["content"]).strip()
    if isinstance(raw.get("legacy_system"), str) and raw["legacy_system"].strip():
        st["legacy_system"] = raw["legacy_system"].strip()

    # ---- 轮 / 主动性 ----
    sums = []
    if ver >= 3 and isinstance(raw.get("recent"), list):
        st["recent"] = [t for t in (_normalize_turn(r) for r in raw["recent"]) if t]
        st["proactive"] = [p for p in (_normalize_proactive(x)
                                       for x in (raw.get("proactive") or [])) if p]
    else:
        msgs = raw.get("recent")
        if not isinstance(msgs, list):
            msgs = raw.get("messages") if isinstance(raw.get("messages"), list) else []
        turns, proa, sums = _messages_to_turns(msgs)
        # 旧 v1/v2 里第一条 system 是「上一版人格」，别当摘要
        if st["legacy_system"]:
            sums = [s for s in sums if s != st["legacy_system"]]
        st["recent"], st["proactive"] = turns, proa

    # ---- 摘要：已有字段优先，绝不拼接（防套娃）----
    rs = raw.get("rolling_summary")
    if not isinstance(rs, str):
        rs = raw.get("summary") if isinstance(raw.get("summary"), str) else ""
    rs = _summary_body(rs)[:SUMMARY_MAX_CHARS]
    if not rs and sums:
        rs = sums[-1][:SUMMARY_MAX_CHARS]
    st["rolling_summary"] = rs

    if isinstance(raw.get("archive"), list):
        arch = raw["archive"]
        if len(arch) > ARCHIVE_MAX_ITEMS:
            _spill_archive(arch[:-ARCHIVE_MAX_ITEMS])
            arch = arch[-ARCHIVE_MAX_ITEMS:]
        st["archive"] = arch
    if isinstance(raw.get("saved_at"), str):
        st["saved_at"] = raw["saved_at"]

    st["schema_version"] = SCHEMA_VERSION
    return st


# ==================== 读写主入口 ====================
def save_state(state=None):
    """把 state 立刻原子落盘。fold 成功后必须调用，防崩溃丢折叠结果。"""
    global _STATE
    st = state if isinstance(state, dict) else _STATE
    if st is None:
        return False
    st["schema_version"] = SCHEMA_VERSION
    st["saved_at"] = _now()
    _STATE = st
    try:
        atomic_write_json(HISTORY_FILE, st)
        return True
    except Exception:
        return False


def load_state(force=False):
    """载入记忆状态（带进程内缓存）。文件坏 / 不存在一律退回默认值，绝不抛。"""
    global _STATE
    if _STATE is not None and not force:
        return _STATE
    raw = None
    if os.path.exists(HISTORY_FILE):
        try:
            raw = _read_json(HISTORY_FILE)
        except Exception:
            raw = None
    _STATE = migrate(raw, HISTORY_FILE) if raw is not None else default_state()
    return _STATE


def to_messages(state, include_proactive=False):
    """state → 可直接喂 API 的 messages 列表。
    注入顺序：人格提示词 → L3 滚动摘要 → 近期对话（严格 user/assistant 交替）。
    proactive 默认不注入（避免出现连续两条 assistant 让接口犯浑），需要时显式打开。"""
    msgs = [{"role": "system", "content": state.get("system") or DEFAULT_SYSTEM_PROMPT}]

    rs = (state.get("rolling_summary") or "").strip()
    if rs:
        msgs.append({"role": "system", "content": _SUMMARY_PREFIX + rs})

    msgs.extend(_iter_flat(state))

    if include_proactive:
        for p in state.get("proactive") or []:
            txt = (p.get("text") or "").strip() if isinstance(p, dict) else ""
            if txt:
                msgs.append({"role": "assistant", "content": txt})
    return msgs


def save_messages(messages):
    """messages 列表 → v3 轮结构 → 原子落盘。各处（对话结束/主动性/退出）都走这里。"""
    global _STATE
    st = load_state()

    system_text = None
    body = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if not content:
            continue
        content = str(content)
        if role == "system":
            # 第一条 system = 人格提示词；锚点块 / 摘要块不许冒充人格。
            if system_text is None:
                system_text = content
            continue
        if role in ("user", "assistant"):
            body.append(m)

    if system_text and not system_text.startswith(("【L1 核心锚点", _SUMMARY_PREFIX)):
        st["system"] = system_text

    turns, proactive, _sums = _messages_to_turns(body)
    st["recent"] = turns

    # 主动性消息累积保存并去重，不覆盖历史
    old = [p for p in (st.get("proactive") or []) if isinstance(p, dict)]
    seen = set((p.get("text") or "") for p in old[-20:])
    for p in proactive:
        if p["text"] in seen:
            continue
        old.append(p)
        seen.add(p["text"])
    st["proactive"] = old[-PROACTIVE_MAX_ITEMS:]

    st["schema_version"] = SCHEMA_VERSION
    st["saved_at"] = _now()
    _STATE = st
    atomic_write_json(HISTORY_FILE, st)
    return True


# ==================== 折叠：只吃 L3，按轮折，摘要唯一 ====================
def needs_fold(state, max_msgs=MAX_HISTORY_MESSAGES):
    """对外仍按「消息条数」判断，内部把轮折算成条数 —— 和 desktop_bug 同口径。"""
    return _total_msg_count(state.get("recent") or []) > max_msgs


def fold(state, summarize_fn, max_msgs=MAX_HISTORY_MESSAGES, keep=KEEP_RECENT_MESSAGES):
    """折叠 L3（按轮）。summarize_fn(payload) -> 新摘要文本。
    旧摘要 + 待折内容 一起喂进去，产出一条新摘要**覆盖**旧的 —— 摘要永远只有一份。
    被折掉的轮摊平进 archive（不销毁，可捞回）。返回 True 表示折叠成功。"""
    recent = [r for r in (state.get("recent") or []) if isinstance(r, dict)]
    if _total_msg_count(recent) <= max_msgs:
        return False

    # 从后往前留够 keep 条消息（half 轮算 1 条）
    tail, acc = [], 0
    for r in reversed(recent):
        c = _turn_msg_count(r)
        if tail and acc + c > keep:
            break
        tail.append(r)
        acc += c
    tail.reverse()
    head = recent[:len(recent) - len(tail)]
    if not head:
        return False

    payload = ""
    old = (state.get("rolling_summary") or "").strip()
    if old:
        payload += "【已有摘要】" + old + "\n\n【待折叠的新对话】\n"
    for r in head:
        u = (r.get("user") or "").strip()
        a = (r.get("assistant") or "").strip()
        if u:
            payload += "用户: %s\n" % u
        if a:
            payload += "路夜: %s\n" % a

    try:
        new_summary = (summarize_fn(payload) or "").strip()
    except Exception:
        return False
    if not new_summary:
        return False
    if len(new_summary) > SUMMARY_MAX_CHARS:
        new_summary = new_summary[:SUMMARY_MAX_CHARS]

    state["rolling_summary"] = new_summary

    items = []
    for r in head:
        ts = r.get("t") or _now()
        if (r.get("user") or "").strip():
            items.append({"ts": ts, "role": "user",
                          "content": r["user"], "reason": "fold"})
        if (r.get("assistant") or "").strip():
            items.append({"ts": ts, "role": "assistant",
                          "content": r["assistant"], "reason": "fold"})
    arch = list(state.get("archive") or [])
    arch.extend(items)
    if len(arch) > ARCHIVE_MAX_ITEMS:
        _spill_archive(arch[:-ARCHIVE_MAX_ITEMS])   # 挤出去的落盘，才真叫「可捞回」
        arch = arch[-ARCHIVE_MAX_ITEMS:]
    state["archive"] = arch
    state["recent"] = tail
    save_state(state)      # 折完立刻落盘，不等下一次 save_messages
    return True


# ==================== L1 / L2 注入块 ====================
def load_core_anchors():
    """L1 锚点正文：优先 core_anchors.md（老大可自改），没有就用代码兜底。"""
    try:
        with open(ANCHORS_FILE, "r", encoding="utf-8") as f:
            txt = f.read().strip()
        if txt:
            return txt
    except Exception:
        pass
    return CORE_ANCHORS_FALLBACK.strip()


def build_anchors_prompt():
    return ("【L1 核心锚点·每轮强制注入，永不压缩】"
            "以下事实优先级高于任何历史摘要与旧提示词：\n" + load_core_anchors())


def load_long_term():
    """L2 议题库。结构：{"topics":[{"title","tags":[...],"summary"}]}"""
    try:
        data = _read_json(LONG_TERM_FILE)
    except Exception:
        return []
    items = data.get("topics") if isinstance(data, dict) else data
    return items if isinstance(items, list) else []


def build_long_term_prompt(user_text, limit=L2_INJECT_MAX):
    """按 tags 关键词命中才注入 L2，平时不占上下文。"""
    if not user_text:
        return ""
    low = str(user_text).lower()
    hit = []
    for it in load_long_term():
        if not isinstance(it, dict):
            continue
        tags = it.get("tags") or []
        if any(str(t).lower() in low for t in tags if t):
            hit.append(it)
        if len(hit) >= limit:
            break
    if not hit:
        return ""
    lines = ["【L2 长期议题·按需加载】以下议题与本次提问相关，供参考，不要求复述："]
    for it in hit:
        title = it.get("title") or "未命名议题"
        summary = (it.get("summary") or "").strip()
        lines.append("- %s：%s" % (title, summary))
    return "\n".join(lines)


def register_topic(title, tags, summary):
    """手动登记 / 更新一条 L2 议题（只增不删，同名覆盖）。"""
    topics = [t for t in load_long_term() if isinstance(t, dict)]
    topics = [t for t in topics if t.get("title") != title]
    topics.append({"title": title, "tags": list(tags or []),
                   "summary": summary, "updated_at": _now()})
    atomic_write_json(LONG_TERM_FILE, {"schema_version": SCHEMA_VERSION, "topics": topics})
    return len(topics)
