# -*- coding: utf-8 -*-
"""
proactive.py —— 路夜的「主动性」引擎（独立模块，可单独运行自测）

设计目标：
1. 随机心跳（30~90 分钟），像"想起来才找你"，不是固定闹钟。
2. 环境感知：看老大鼠标下 / 前台是什么程序、在不在全屏、多久没动键鼠。
3. 三道门：安静够久 + 随机概率 + 当前没在聊，全过才开口。
4. 话题贴着老大当下在做的事；说什么由模型现场生成，不写死台词。
5. 全屏独占游戏：不弹（怕把游戏切出去），攒到退出那一刻再发，并标注「游玩结束」。
   窗口化/无边框游戏：可以中途搭话（问玩了多久、好不好玩），随会话时长变话题。

隐私红线：
- 只读进程名 + 窗口标题，绝不截图、绝不读窗口内容、绝不落盘。
- 聊天类窗口标题自动打码，只报"在聊天"，不报"和谁聊"。

自测：
    python proactive.py            # 打印一次环境感知结果
    python proactive.py --sense    # 同上，并给出游戏会话事件
    python proactive.py --generate # 真的调一次模型，看看会说啥
    python proactive.py --dry      # 只跑一次决策，不发送
"""
import os
import sys
import json
import time
import random
import datetime

try:
    import requests
except Exception:
    requests = None

try:
    import psutil
except Exception:
    psutil = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except Exception:
    pass

STATE_FILE = os.path.join(BASE_DIR, "proactive_state.json")
TODO_FILE = os.path.join(BASE_DIR, "next_todo.md")
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")

# ==================== 可调参数（老大拍过的数） ====================
HEARTBEAT_MIN_SEC = 30 * 60          # 心跳最短 30 分钟
HEARTBEAT_MAX_SEC = 90 * 60          # 心跳最长 90 分钟
DAILY_LIMIT = 32                     # 每日唤醒上限（16~48 取中）
BASE_PROB = 0.5                      # 基准开口概率
QUIET_HOUR_START = 23                # 免打扰起点
QUIET_HOUR_END = 8                   # 免打扰终点
QUIET_FACTOR = 0.2                   # 深夜概率打折（不全禁）
RECENT_REPLY_WINDOW = 30 * 60        # "刚聊过"的时间窗
RECENT_REPLY_FACTOR = 0.4
CHAT_OPEN_FACTOR = 1.5               # 窗口开着，说了立刻能看见
TODO_FACTOR = 1.3                    # 有旧账，更该开口
GAME_WINDOWED_FACTOR = 0.6           # 游戏进行中，收敛一点
GAME_MID_PROB = 0.5                  # 窗口化游戏中途搭话的基准概率
IDLE_MIN_SEC = 20 * 60               # 安静够久才算（20 分钟）
GAME_TALK_MIN_MINUTES = 40           # 游戏内最短搭话时长（低于这个闭嘴）
GAME_TALK_MAX_PER_SESSION = 2        # 同一局最多搭话 2 次
GAME_SESSION_MIN_MINUTES = 5         # 短于一局 5 分钟的不播报
TICK_SEC = 60                        # 循环节拍

# ---- 搜索省钱策略（Tavily 免费版 1000 次/月，别乱花）----
SEARCH_MODE = (os.environ.get("PROACTIVE_SEARCH") or "off").strip().lower()  # off/auto/always
SEARCH_DAILY_LIMIT = 3               # 每日主动搜索硬上限（0 = 不限）
SEARCH_CACHE_FILE = os.path.join(BASE_DIR, "search_cache.json")

# ==================== Windows 环境感知（纯 ctypes） ====================
_HAS_WIN32 = False
ctypes = None
wintypes = None
_user32 = _kernel32 = None
try:
    import ctypes as _ctypes_mod
    from ctypes import wintypes as _wt
    ctypes = _ctypes_mod
    wintypes = _wt
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _HAS_WIN32 = True
except Exception:
    _HAS_WIN32 = False


if _HAS_WIN32:
    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    class _RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class _POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def get_idle_seconds():
    """距上次动键盘/鼠标过了多少秒。"""
    if not _HAS_WIN32:
        return 0.0
    try:
        li = _LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(li)
        if not _user32.GetLastInputInfo(ctypes.byref(li)):
            return 0.0
        return max(0.0, (_kernel32.GetTickCount() - li.dwTime) / 1000.0)
    except Exception:
        return 0.0


def _title_of(hwnd):
    try:
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return ""


def _pid_of(hwnd):
    try:
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value
    except Exception:
        return 0


def _proc_name(pid):
    if not psutil or not pid:
        return ""
    try:
        return psutil.Process(pid).name()
    except Exception:
        return ""


def _root(hwnd):
    try:
        return _user32.GetAncestor(hwnd, 2) or hwnd   # GA_ROOT
    except Exception:
        return hwnd


def is_fullscreen(hwnd):
    if not _HAS_WIN32:
        return False
    try:
        r = _RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return False
        sw, sh = _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
        w, h = r.right - r.left, r.bottom - r.top
        if sw <= 0 or sh <= 0 or w <= 0 or h <= 0:
            return False
        return w >= sw - 2 and h >= sh - 2
    except Exception:
        return False


# -------------------- 进程分类 --------------------
CHAT_PROCS = {"wechat.exe", "weixin.exe", "qq.exe", "qqnt.exe", "tim.exe",
              "dingtalk.exe", "wxwork.exe", "telegram.exe", "discord.exe",
              "feishu.exe", "lark.exe", "slack.exe"}
CODE_PROCS = {"code.exe", "code - insiders.exe", "pycharm64.exe", "pycharm.exe",
              "devenv.exe", "idea64.exe", "goland64.exe", "webstorm64.exe",
              "windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
              "python.exe", "pythonw.exe", "sublime_text.exe", "notepad++.exe",
              "cursor.exe", "windsurf.exe"}
BROWSER_PROCS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
                 "360se.exe", "360chrome.exe", "sogouexplorer.exe", "iexplore.exe"}
MEDIA_PROCS = {"vlc.exe", "potplayermini64.exe", "potplayer.exe", "mpc-hc64.exe",
               "mpc-hc.exe", "foobar2000.exe", "qqmusic.exe", "cloudmusic.exe",
               "spotify.exe", "bilibili.exe", "migu.exe", "wmplayer.exe"}
OFFICE_PROCS = {"winword.exe", "excel.exe", "powerpnt.exe", "wps.exe", "et.exe",
                "wpp.exe", "onenote.exe", "acrobat.exe", "acrord32.exe", "sumatrapdf.exe"}
NON_GAME_PROCS = CHAT_PROCS | CODE_PROCS | BROWSER_PROCS | MEDIA_PROCS | OFFICE_PROCS | {
    "explorer.exe", "dwm.exe", "taskmgr.exe", "snippingtool.exe", "searchapp.exe"}

GAME_HINTS = ("steam", "epicgames", "riot", "valorant", "league of legends", "leagueclient",
              "dota2", "csgo", "cs2", "genshin", "yuanshen", "honkai", "starrail", "zenless",
              "minecraft", "javaw.exe", "hoi4", "stardew", "terraria", "eldenring",
              "witcher", "cyberpunk", "baldurs", "battle.net", "battlefield", "apex",
              "pubg", "naraka", "deltaforce", "uplay", "ubisoft", "rockstar", "gta",
              "rdr2", "hades", "deadcells", "slaythespire", "raint risk", "rr2", "wuthering",
              "arknights", "bluearchive", "nikke", "p5r", "tearsofthekingdom", "yuzu", "ryujinx",
              "factorio", "oxygen", "rimworld", "cities", "anno", "farcry", "assassin", "nfs")
GAME_TITLE_HINTS = ("原神", "崩坏", "星穹铁道", "绝区零", "鸣潮", "明日方舟", "碧蓝档案",
                    "三角洲行动", "永劫无间", "英雄联盟", "王者荣耀", "穿越火线", "地下城与勇士",
                    "我的世界", "泰拉瑞亚", "星露谷", "饥荒", "文明", "全面战争", "欧陆风云",
                    "钢铁雄心", "群星", "杀戮尖塔", "死亡细胞", "哈迪斯", "幻兽帕鲁",
                    "黑神话", "悟空", "赛博朋克", "巫师", "艾尔登法环", "只狼", "塞尔达")


def _classify(proc, title, fullscreen):
    p = (proc or "").lower()
    if p in ("python.exe", "pythonw.exe") and "路夜" in (title or ""):
        return "self"
    if p in CHAT_PROCS:
        return "chat"
    if p in CODE_PROCS:
        return "code"
    if p in BROWSER_PROCS:
        return "browser"
    if p in MEDIA_PROCS:
        return "media"
    if p in OFFICE_PROCS:
        return "office"
    if is_game(proc, title, fullscreen):
        return "game"
    return "other"


def is_game(proc, title, fullscreen):
    p = (proc or "").lower()
    if not p:
        return False
    if p in NON_GAME_PROCS:
        return False
    if any(h in p for h in GAME_HINTS):
        return True
    if any(h in (title or "") for h in GAME_TITLE_HINTS):
        return True
    return bool(fullscreen)     # 全屏 + 不在白名单 = 可疑游戏


def _mask_title(kind, title):
    """聊天类窗口标题打码，只报"在聊天"，不报"和谁聊"。"""
    if kind == "chat":
        return "（聊天类窗口，标题已打码）"
    return (title or "").strip()


def sense_environment():
    """一次环境快照。不截图、不读内容、不落盘。"""
    env = {
        "ts": time.time(),
        "idle_sec": 0.0,
        "fullscreen": False,
        "fore": {"proc": "", "title": "", "kind": ""},
        "cursor": {"proc": "", "title": ""},
        "game": False,
    }
    if not _HAS_WIN32:
        return env

    env["idle_sec"] = get_idle_seconds()

    try:
        fg = _user32.GetForegroundWindow()
        fg_root = _root(fg)
        env["fullscreen"] = is_fullscreen(fg_root)
        f_proc = _proc_name(_pid_of(fg_root))
        f_title = _title_of(fg_root)
        f_kind = _classify(f_proc, f_title, env["fullscreen"])
        env["fore"] = {"proc": f_proc, "title": _mask_title(f_kind, f_title), "kind": f_kind}
    except Exception:
        pass

    try:
        pt = _POINT()
        if _user32.GetCursorPos(ctypes.byref(pt)):
            hwnd = _root(_user32.WindowFromPoint(_POINT(pt.x, pt.y)))
            c_proc = _proc_name(_pid_of(hwnd))
            c_title = _title_of(hwnd)
            c_kind = _classify(c_proc, c_title, False)
            env["cursor"] = {"proc": c_proc, "title": _mask_title(c_kind, c_title)}
    except Exception:
        pass

    return env


# ==================== 游戏会话追踪 ====================
_current_game = {"proc": None, "title": "", "start": 0.0, "talked": 0}


def is_process_running(name):
    """游戏进程是否还活着。用来区分"切出去看攻略"和"真的退出了"。"""
    if not psutil or not name:
        return False
    try:
        low = name.lower()
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() == low:
                return True
    except Exception:
        pass
    return False


def update_game_session(env):
    """维护游戏会话，返回事件：playing / background / ended / ignored / none。"""
    now = env["ts"]
    fore = env.get("fore") or {}
    is_g = fore.get("kind") == "game"

    if is_g:
        if _current_game["proc"] != fore.get("proc"):
            _current_game.update({"proc": fore.get("proc"), "title": fore.get("title") or "",
                                  "start": now, "talked": 0})
            env["game"] = {"name": fore.get("proc"), "title": _current_game["title"],
                           "minutes": 0.0, "fullscreen": env.get("fullscreen", False)}
            return "playing"
        _current_game["title"] = fore.get("title") or _current_game["title"]
        minutes = (now - _current_game["start"]) / 60.0
        env["game"] = {"name": _current_game["proc"], "title": _current_game["title"],
                       "minutes": minutes, "fullscreen": env.get("fullscreen", False)}
        return "playing"

    if _current_game["proc"]:
        if is_process_running(_current_game["proc"]):
            # 只是切出来了（看攻略/回消息），游戏还在后台跑 —— 不算结束
            env["game"] = False
            return "background"
        minutes = (now - _current_game["start"]) / 60.0
        ended = {"name": _current_game["proc"], "title": _current_game["title"],
                 "minutes": minutes, "just_ended": True, "fullscreen": False}
        _current_game.update({"proc": None, "title": "", "start": 0.0, "talked": 0})
        env["game"] = ended
        return "ended" if minutes >= GAME_SESSION_MIN_MINUTES else "ignored"

    env["game"] = False
    return "none"


# ==================== 状态持久化 ====================
def load_state():
    today = datetime.date.today().isoformat()
    st = {"date": today, "count": 0, "last_sent": 0.0}
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                st.update(d)
    except Exception:
        pass
    if st.get("date") != today:
        st["date"] = today
        st["count"] = 0
    return st


def save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception:
        pass


def read_todos(n=2):
    """读 next_todo.md 的「今日」段前 n 条，当旧账。"""
    if not os.path.exists(TODO_FILE):
        return []
    items = []
    in_today = False
    try:
        with open(TODO_FILE, "r", encoding="utf-8") as f:
            for line in f.read().splitlines():
                s = line.strip()
                if s.startswith("## "):
                    in_today = ("今日" in s[3:].strip())
                    continue
                if not in_today or not s or s.startswith("<!--"):
                    continue
                items.append(s.lstrip("-*+ ").strip())
                if len(items) >= n:
                    break
    except Exception:
        pass
    return items


def last_reply_ts():
    try:
        return os.path.getmtime(HISTORY_FILE)
    except Exception:
        return 0.0


def collect_context(app, st):
    now = time.time()
    lr = last_reply_ts()
    return {
        "chat_open": bool(getattr(app, "chat_open_flag", False)) if app is not None else False,
        "typing": bool(getattr(app, "is_typing", False)) if app is not None else False,
        "recent_reply": (now - lr) < RECENT_REPLY_WINDOW if lr else False,
        "has_todo": bool(read_todos(1)),
    }


# ==================== 概率门控 ====================
def compute_prob(env, ctx):
    p = BASE_PROB
    hour = datetime.datetime.now().hour
    if hour >= QUIET_HOUR_START or hour < QUIET_HOUR_END:
        p *= QUIET_FACTOR
    if ctx.get("recent_reply"):
        p *= RECENT_REPLY_FACTOR
    if ctx.get("chat_open"):
        p *= CHAT_OPEN_FACTOR
    if ctx.get("has_todo"):
        p *= TODO_FACTOR
    g = env.get("game")
    if isinstance(g, dict) and not g.get("just_ended"):
        p *= GAME_WINDOWED_FACTOR
    return max(0.0, min(1.0, p))


# ==================== 提示词与生成 ====================
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


_KIND_CN = {
    "self": "在用路夜",
    "code": "在写代码",
    "browser": "在浏览网页",
    "chat": "在聊天",
    "media": "在看视频/听歌",
    "office": "在处理文档",
    "game": "在打游戏",
    "other": "在用电脑",
}


def describe_env(env):
    fore = env.get("fore") or {}
    cursor = env.get("cursor") or {}
    kind = fore.get("kind") or ""
    task = _KIND_CN.get(kind, "在用电脑")
    lines = [f"- 老大现在{task}（前台程序 {fore.get('proc') or '未知'}，窗口标题：{fore.get('title') or '无'}）"]
    if cursor.get("proc") and cursor.get("proc") != fore.get("proc"):
        lines.append(f"- 鼠标停在 {cursor.get('proc')}（标题：{cursor.get('title') or '无'}）")
    idle_min = int(env.get("idle_sec", 0) // 60)
    lines.append(f"- 已经安静了约 {idle_min} 分钟")
    todos = read_todos(2)
    if todos:
        lines.append("- 手头旧账：" + "；".join(todos))
    return "\n".join(lines)


def build_prompt(env, reason):
    now = datetime.datetime.now()
    head = ""
    if reason == "game_end":
        g = env.get("game") or {}
        head = (
            "[主动性·游玩结束]\n"
            f"现在是 {now:%Y-%m-%d %H:%M}（{time_slot(now)}）。\n"
            f"老大刚刚结束了一局游戏：《{g.get('title') or g.get('name')}》，"
            f"这一局玩了大约 {int(g.get('minutes') or 0)} 分钟。\n"
            "【重点】游戏已经退出了，是过去时。不要写成「正在玩」的语气，"
            "也不要问「要不要一起玩」。要像「看你刚打完」那样，自然收个尾、聊一句战况或感受。\n"
        )
    elif reason == "game_mid":
        g = env.get("game") or {}
        head = (
            "[主动性·游戏中]\n"
            f"现在是 {now:%Y-%m-%d %H:%M}（{time_slot(now)}）。\n"
            f"老大正在玩《{g.get('title') or g.get('name')}》，已经玩了大约 {int(g.get('minutes') or 0)} 分钟。\n"
        )
    else:
        head = (
            "[主动性·日常]\n"
            f"现在是 {now:%Y-%m-%d %H:%M}（{time_slot(now)}）。\n"
            + describe_env(env) + "\n"
        )

    rules = (
        "\n要求：\n"
        "1. 用一两句话主动搭话，开头叫「老大」，只喊一次。\n"
        "2. 必须贴着老大当下在做的事说，别用『在忙吗』这类万能寒暄。\n"
        "3. 最多问一个问题，不列清单、不碎碎念、不连续追问。\n" +
        ("4. 如果窗口标题是英文缩写或看不懂的游戏代号，"
         "可以先用 web_search 查一下这是什么游戏再开口（最多搜一次）。\n"
         if _search_on() else
         "4. 不要使用搜索工具；标题看不懂就照原样自然地提一句，别硬猜、别解释。\n") +
        "5. 直接输出要发给老大的那句话本身，不要任何前缀、标签或解释。\n"
    )
    return head + rules


SEARCH_TOOL = [{
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "联网搜索。当游戏标题是看不懂的英文缩写/代号时，用来确认这是什么游戏。",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
}]

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"
TIMEOUT = 25


def _search_on():
    """主动搭话时到底允不允许联网搜索（默认 off，省 Tavily 额度）。"""
    return SEARCH_MODE in ("auto", "always")


def _load_cache():
    try:
        with open(SEARCH_CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_cache(d):
    try:
        with open(SEARCH_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _track_file():
    return os.path.join(BASE_DIR, "search_count.json")


def _today_search_count():
    try:
        with open(_track_file(), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return 0
    if d.get("date") != datetime.date.today().isoformat():
        return 0
    try:
        return int(d.get("count", 0))
    except Exception:
        return 0


def _bump_search_count():
    try:
        with open(_track_file(), "w", encoding="utf-8") as f:
            json.dump({"date": datetime.date.today().isoformat(),
                       "count": _today_search_count() + 1}, f)
    except Exception:
        pass


def _search(query):
    if not requests:
        return "搜索不可用（缺少 requests）"
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return "未配置 TAVILY_API_KEY，无法搜索"
    ck = (query or "").strip().lower()
    cache = _load_cache()
    if ck and ck in cache:            # 同一个名字只花一次额度
        return cache[ck]
    if SEARCH_DAILY_LIMIT and _today_search_count() >= SEARCH_DAILY_LIMIT:
        return "今日搜索额度已用完：请直接照标题原样自然地说，不要再提搜索"
    try:
        r = requests.post("https://api.tavily.com/search",
                          json={"api_key": key, "query": query, "max_results": 3}, timeout=10)
        r.raise_for_status()
        res = [{"title": x.get("title", ""), "content": x.get("content", "")[:300]}
               for x in r.json().get("results", [])]
        out = json.dumps(res, ensure_ascii=False)
        if ck:
            cache[ck] = out
            _save_cache(cache)
        _bump_search_count()
        return out
    except Exception as e:
        return f"搜索失败: {e}"


def call_model(prompt, allow_search=True):
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key or not requests:
        return None
    msgs = [{"role": "user", "content": prompt}]
    for _ in range(3):
        payload = {"model": MODEL, "messages": msgs, "stream": False}
        if allow_search:
            payload["tools"] = SEARCH_TOOL
        try:
            resp = requests.post(API_URL, json=payload,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"},
                                 timeout=TIMEOUT)
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
        except Exception:
            return None

        tcs = msg.get("tool_calls")
        if tcs:
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tcs})
            for tc in tcs:
                try:
                    args = json.loads(tc.get("function", {}).get("arguments") or "{}")
                except Exception:
                    args = {}
                msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "content": _search(args.get("query", ""))})
            continue
        text = (msg.get("content") or "").strip()
        return text or None
    return None


def _clean(text):
    if not text:
        return ""
    t = text.strip().strip('"').strip()
    for pre in ("路夜：", "路夜:", "【主动性】", "【主动性·游玩结束】",
                "【主动性·游戏中】", "【主动性·日常】"):
        if t.startswith(pre):
            t = t[len(pre):].strip()
    return t


def generate(env, reason):
    text = call_model(build_prompt(env, reason), allow_search=_search_on())
    return _clean(text)


# ==================== 决策 ====================
def _pick_reason(env, event, ctx, st, next_fire):
    if int(st.get("count", 0)) >= DAILY_LIMIT:
        return None
    if ctx.get("typing"):
        return None

    # 1) 游戏刚结束：立刻补上（不受随机间隔、不受空闲门槛限制）
    if event == "ended":
        return "game_end"

    g = env.get("game")

    # 2) 窗口化游戏进行中：到点可搭话
    if event == "playing" and isinstance(g, dict) and not g.get("fullscreen"):
        if (g.get("minutes", 0) >= GAME_TALK_MIN_MINUTES
                and _current_game["talked"] < GAME_TALK_MAX_PER_SESSION
                and time.time() >= next_fire):
            p = min(1.0, GAME_MID_PROB * (QUIET_FACTOR if _is_quiet_hour() else 1.0))
            if random.random() < p:
                return "game_mid"

    # 3) 全屏独占游戏：攒着，不打扰
    if isinstance(g, dict) and g.get("fullscreen"):
        return None

    # 4) 常规心跳
    if time.time() >= next_fire:
        if env.get("idle_sec", 0) < IDLE_MIN_SEC:
            return None
        if random.random() < compute_prob(env, ctx):
            return "daily"
    return None


def _is_quiet_hour():
    h = datetime.datetime.now().hour
    return h >= QUIET_HOUR_START or h < QUIET_HOUR_END


def deliver(push_cb, app, text, reason):
    text = _clean(text)
    if not text:
        return False
    meta = {"reason": reason}
    try:
        if push_cb:
            push_cb(text, meta)
        elif app is not None:
            app.root.after(0, lambda: app.push_proactive(text, meta))
        else:
            print(text)
        return True
    except Exception:
        return False


# ==================== 主循环 ====================
def worker(app=None, push_cb=None, tick_sec=TICK_SEC):
    """后台线程主循环。app 为 DesktopBugApp 实例；push_cb(text, meta) 负责真正把消息送出去。"""
    st = load_state()
    next_fire = time.time() + random.randint(HEARTBEAT_MIN_SEC, HEARTBEAT_MAX_SEC)
    while True:
        try:
            env = sense_environment()
            event = update_game_session(env)
            ctx = collect_context(app, st)
            reason = _pick_reason(env, event, ctx, st, next_fire)
            if reason:
                text = generate(env, reason)
                if text and deliver(push_cb, app, text, reason):
                    st["count"] = int(st.get("count", 0)) + 1
                    st["last_sent"] = time.time()
                    save_state(st)
                    if reason == "game_mid":
                        _current_game["talked"] += 1
            if time.time() >= next_fire:
                next_fire = time.time() + random.randint(HEARTBEAT_MIN_SEC, HEARTBEAT_MAX_SEC)
        except Exception:
            pass
        time.sleep(tick_sec)


# ==================== 自测入口 ====================
def _selftest():
    env = sense_environment()
    event = update_game_session(env)
    if "--generate" in sys.argv:
        print("[环境]", json.dumps(env, ensure_ascii=False))
        print("[事件]", event)
        print("[生成]", generate(env, "daily"))
        return
    if "--dry" in sys.argv:
        st = load_state()
        ctx = collect_context(None, st)
        print("[环境]", json.dumps(env, ensure_ascii=False, indent=2))
        print("[事件]", event)
        print("[上下文]", json.dumps(ctx, ensure_ascii=False))
        print("[概率]", compute_prob(env, ctx))
        print("[提示词]\n" + build_prompt(env, "daily"))
        return
    print(json.dumps(env, ensure_ascii=False, indent=2))
    if "--sense" in sys.argv:
        print("[事件]", event)


if __name__ == "__main__":
    _selftest()
