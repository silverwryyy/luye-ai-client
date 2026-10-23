# -*- coding: utf-8 -*-
"""
desktop_fixed2.py —— 路夜（修复版 v2：窗口布局修复）
基于 desktop_fixed.py 修改，只动了 open_chat_window 里 3 处：

1. Toplevel 的 geometry 从 "480x640" 改为带位置 + minsize，
   保证窗口不会因为父窗口在屏幕右下角而把底部挤出屏幕。
2. ScrolledText 显式指定 height=12，不再隐式预订 24 行高度。
3. 调整 pack 顺序：input_frame 先 pack 并 side=BOTTOM（钉死底部），
   chat_text_area 后 pack 并 side=TOP，窗口变小时缩的是聊天区，输入框永不被挤掉。

其余逻辑（工具调用、记忆折叠、防偷懒拦截、单实例锁 54322）与 desktop_fixed.py 完全一致。
注意：本文件会 chdir 到自己所在目录，bug.png 需要和它放在同一个文件夹。
"""
import os
import sys
import json
import re
import random
import time
import queue
import socket
import subprocess
import datetime
import threading
import traceback
import requests
import psutil
import tkinter as tk
from tkinter import scrolledtext, messagebox
from dotenv import load_dotenv
from openai import OpenAI
from winotify import Notification, audio

# 1. 初始化环境与防弹衣
if sys.stdout is not None:
    sys.stdout.reconfigure(encoding='utf-8')

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ==================== 崩溃取证（telemetry）====================
# 路夜跑在 pythonw.exe 下，进程被系统硬杀（0xC0000005 / 磁盘读取失败）时
# 没有任何 Python 异常可捕获，error.log 一个字节都写不出来。
# faulthandler 直接挂在信号/SEH 上，硬崩那一刻能把每个线程的栈刷进
# crash_trace.log；心跳文件则保证"死于几点几分"永远查得到。
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_CRASH_FILE = os.path.join(_BASE_DIR, "crash_trace.log")
_HEARTBEAT_FILE = os.path.join(_BASE_DIR, "runtime_heartbeat.txt")
_LAUNCH_FILE = os.path.join(_BASE_DIR, "launch_trace.log")


def _trace(msg):
    try:
        with open(_LAUNCH_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] pid=%d %s\n" % (
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), msg))
    except Exception:
        pass


def _heartbeat_loop():
    while True:
        try:
            with open(_HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                f.write("%s alive pid=%d\n" % (
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass
        time.sleep(30)


try:
    import faulthandler as _faulthandler
    _crash_fp = open(_CRASH_FILE, "ab", buffering=0)
    _crash_fp.write(("\n===== 启动 %s pid=%d =====\n" % (
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), os.getpid())).encode("utf-8"))
    _faulthandler.enable(file=_crash_fp, all_threads=True)
except Exception:
    pass

try:
    import atexit as _atexit
    _atexit.register(lambda: _trace("正常退出"))
except Exception:
    pass

_trace("启动 python=%s" % sys.version.split()[0])
threading.Thread(target=_heartbeat_loop, daemon=True).start()
# ==================== /崩溃取证 ====================

load_dotenv()

client = OpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com"
)

message_queue = queue.Queue()
alerted_pids = set()

# ==================== P2: 强行中断 ====================
# 每轮发送开头 clear()；点「■ 停止」时 set()。
# 流式循环逐 chunk 查、工具轮开头查；命中即停。
# 硬限制：create(stream=False) 那次非流式调用拦不住，只能等它返回后再查标记。
INTERRUPT_EVENT = threading.Event()


class Interrupted(Exception):
    """老大按下停止键时从生成流程里抛出，走单独分支收尾。"""
    pass

# ==================== 记忆持久化与折叠机制 ====================
try:
    import memory
    DEFAULT_SYSTEM_PROMPT = memory.DEFAULT_SYSTEM_PROMPT
except Exception:
    memory = None
    DEFAULT_SYSTEM_PROMPT = ("你是老大的赛博助手，名叫路夜。回答要简短精干；必须调用工具去执行操作，"
                             "分块读文件直到读完，写文件前先说清路径与内容，严禁写系统盘核心文件！")

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json")
SAVE_ERROR_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "save_error.log")

MAX_HISTORY_MESSAGES = 20
KEEP_RECENT_MESSAGES = 8

def load_history():
    # v1.2: 走三层记忆引擎 memory.py；只有它不可用时才退回旧兜底。
    if memory is not None:
        try:
            return memory.to_messages(memory.load_state())
        except Exception as e:
            log_hit("memory_load_fail", str(e), "")
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    return data
        except Exception:
            pass
    return [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}]


def save_history(messages):
    # v1.2: 原子写(.tmp + os.replace) + v2 结构；写一半崩也不会毁掉记忆。
    if memory is not None:
        try:
            memory.save_messages(messages)
            return
        except Exception as e:
            log_hit("memory_save_fail", str(e), "")
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        try:
            with open(SAVE_ERROR_LOG, "a", encoding="utf-8") as log:
                log.write(f"[{datetime.datetime.now()}] [保存历史失败] {e}\n")
        except Exception:
            pass


def check_and_fold_history(messages_context):
    # v1.2 折叠边界：只吃 L3(recent)；L1 核心锚点与 L2 长期议题永不进摘要。
    # 摘要为「唯一一条滚动摘要」：旧摘要并入新内容后覆盖，绝不层层套娃。
    _sys = sum(1 for m in messages_context if isinstance(m, dict) and m.get("role") == "system")
    if (len(messages_context) - _sys) <= MAX_HISTORY_MESSAGES:
        return
    if memory is None:
        return

    message_queue.put(("AI_CHUNK", "\n[🧠 记忆超载，正在整理 L3 上下文，结果稍后报告...]\n"))

    def _summarize(payload):
        prompt = ("请把下面这段历史对话压缩成一段不超过 800 字的摘要，"
                  "保留用户的核心诉求、AI 已完成的动作和关键结论。"
                  "已写进【已有摘要】的内容请并入，不要重复罗列。\n" + payload)
        resp = client.chat.completions.create(
            model="deepseek-flash",
            messages=[{"role": "user", "content": prompt}],
            stream=False
        )
        return resp.choices[0].message.content

    try:
        state = memory.load_state()
        ok = memory.fold(state, _summarize, MAX_HISTORY_MESSAGES, KEEP_RECENT_MESSAGES)
        folded = memory.to_messages(state)
        messages_context.clear()
        messages_context.extend(folded)
        if ok:
            message_queue.put(("AI_CHUNK", "\n[✅ L3 已折叠并落盘；archive 满 300 条后的旧条目存入 _backup/archive_spill.jsonl。]\n"))
        else:
            message_queue.put(("AI_CHUNK", "\n[⚠️ 折叠未成（摘要为空或调用异常），摘要未改动，仅按最近 %d 条裁剪上下文。]\n" % KEEP_RECENT_MESSAGES))
    except Exception as e:
        log_hit("fold_fail", str(e), "")

# ==================== 后台逻辑模块 ====================
def clean_query(query: str) -> str:
    query = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9\s]', ' ', query)
    return re.sub(r'\s+', ' ', query).strip()

def looks_like_question(text: str) -> bool:
    """判断这句话是不是在提问/商量——是的话就不是"光说不做"，拦截器应当放行。"""
    if not text:
        return False
    markers = ["？", "?", "要不要", "可以吗", "好吗", "行吗", "需要我", "是否", "还是说", "你说", "？\n"]
    return any(m in text for m in markers)

def web_search(query: str):
    query = clean_query(query)
    message_queue.put(("AI_CHUNK", f"\n[🛰️ 路夜正在联网搜索: {query}...]"))
    try:
        url = "https://api.tavily.com/search"
        payload = {"api_key": os.environ.get("TAVILY_API_KEY"), "query": query, "max_results": 3}
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        results = [{"title": r.get("title", ""), "content": r.get("content", "")[:500]} for r in response.json().get("results", [])]
        return json.dumps(results, ensure_ascii=False)
    except Exception as e:
        return f"搜索失败: {str(e)}"

def list_directory(path: str):
    path = path.replace('\\', '/').strip().strip('"').strip("'")
    message_queue.put(("AI_CHUNK", f"\n[📂 路夜正在扫描目录: {path}...]"))
    try:
        if not path or path == ".":
            path = os.getcwd()
        if not os.path.exists(path):
            return f"路径不存在: {path}"
        items = [i for i in os.listdir(path) if not i.startswith('.') and i != '__pycache__' and i != '.venv']
        return f"目录 '{path}' 下的内容有: {', '.join(items)}"
    except Exception as e:
        return f"扫描目录失败: {str(e)}"

def read_file(filepath: str, start_line: int = 1, end_line: int = 200):
    filepath = filepath.replace('\\', '/').strip().strip('"').strip("'")
    message_queue.put(("AI_CHUNK", f"\n[📄 路夜正在读取文件: {filepath} (第{start_line}-{end_line}行)...]"))
    try:
        if not os.path.exists(filepath):
            return f"找不到文件: {filepath}"

        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        total_lines = len(lines)

        if start_line < 1: start_line = 1
        if end_line > total_lines: end_line = total_lines
        if start_line > end_line:
            return f"参数错误：起始行 {start_line} 大于结束行 {end_line}"

        if end_line - start_line > 400:
            end_line = start_line + 400

        chunk = "".join(lines[start_line-1 : end_line])

        result = f"文件总行数: {total_lines}。以下是第 {start_line} 到 {end_line} 行的内容：\n{chunk}\n"
        if end_line < total_lines:
            result += f"\n[⚠️ 强制要求：文件尚未读完！请立即再次调用 read_file 读取第 {end_line+1} 行到第 {min(total_lines, end_line+400)} 行！不要停下！]"

        return result
    except Exception as e:
        return f"读取文件失败: {str(e)}"

def read_word(filepath, start_line=1, end_line=None):
    import zipfile, re
    try:
        with zipfile.ZipFile(filepath) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    except Exception as e:
        return f"[读取失败] {e}"

    xml = xml.replace("</w:p>", "</w:p>\n")
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    lines = text.split("\n")

    total = len(lines)
    s = max(1, start_line)
    e = min(end_line or total, total)
    body = "\n".join(lines[s-1:e])
    if e < total:
        body += f"\n\n[已读 {s}-{e} 行，共 {total} 行，后续还有内容，请继续读取]"
    return body

# ---------- 补丁⑩: 写入路径安全闸门(is_path_safe) ----------
# write_file / write_word 是直接落盘的（不像 run_command 会弹窗确认），
# 所以必须在函数内部先过闸门，挡住「AI 犯浑写系统盘」这类事故。

_WRITE_DENY_EXT = {
    ".dll", ".sys", ".exe", ".msi", ".ocx", ".cpl", ".scr", ".drv",
    ".efi", ".mui", ".cat", ".inf", ".com",
}


def _write_deny_roots():
    """运行时用环境变量拼黑名单目录，兼容系统盘不是 C: 的机器。"""
    def _env(name, default):
        return (os.environ.get(name) or default).replace("\\", "/").lower().rstrip("/")

    win = _env("SystemRoot", "C:/Windows")
    drv = _env("SystemDrive", "C:").rstrip(":")
    roots = [
        win,
        _env("ProgramFiles", "C:/Program Files"),
        _env("ProgramFiles(x86)", "C:/Program Files (x86)"),
        _env("ProgramData", "C:/ProgramData"),
        drv + ":/$recycle.bin",
        drv + ":/system volume information",
        drv + ":/boot",
        drv + ":/recovery",
        drv + ":/perflogs",
        drv + ":/msocache",
        drv + ":/config.msi",
    ]
    return [r for r in roots if r]


def is_path_safe(filepath):
    """写入前闸门：返回 (ok, reason)。ok=False 一律拒绝写入。"""
    raw = str(filepath or "").strip().strip('"').strip("'")
    if not raw:
        return False, "路径为空"
    try:
        p = os.path.realpath(os.path.abspath(os.path.expandvars(os.path.expanduser(raw))))
    except Exception:
        p = raw
    norm = p.replace("\\", "/").rstrip("/")
    low = norm.lower()

    if os.path.isdir(p):
        return False, "目标是目录而不是文件: " + norm

    ext = os.path.splitext(low)[1]
    if ext in _WRITE_DENY_EXT:
        return False, "系统二进制/可执行扩展名(%s)禁止写入: %s" % (ext, norm)

    for root in _write_deny_roots():
        if low == root or low.startswith(root + "/"):
            return False, "命中系统核心目录(%s): %s" % (root, norm)

    sysdrv = (os.environ.get("SystemDrive") or "C:").lower().strip(":")
    if re.match(r"^[a-z]:/[^/]*$", low) and low.split("/")[0].startswith(sysdrv):
        return False, "禁止直接往系统盘根目录写文件: " + norm

    return True, norm


def write_word(filepath, blocks, mode="new"):
    """创建/追加 Word(.docx)。
    blocks = [
        {"type": "h1", "text": "标题"},
        {"type": "p",  "text": "正文"},
        {"type": "table", "rows": [["队伍","学生"],["下午一队","杨刚"]]},
    ]
    mode: "new"=新建覆盖, "append"=在已有文档后追加
    """
    from docx import Document
    from docx.oxml.ns import qn

    filepath = filepath.replace('\\', '/').strip().strip('"').strip("'")
    _safe, _reason = is_path_safe(filepath)
    if not _safe:
        log_hit("write_denied", _reason, filepath)
        return "[已拒绝] 写入未通过路径安全校验：%s。请换到桌面/工作空间等安全路径。" % _reason
    message_queue.put(("AI_CHUNK", f"\n[📝 路夜正在写Word: {filepath}...]"))
    try:
        if mode == "append" and os.path.exists(filepath):
            doc = Document(filepath)
        else:
            doc = Document()

        def _set_font(run):
            # 中文必须设 eastAsia, 否则字体回退成宋体/乱码
            run.font.name = "微软雅黑"
            run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

        for b in blocks:
            t = b.get("type", "p")
            if t == "table":
                rows = b.get("rows", [])
                if not rows:
                    continue
                tb = doc.add_table(rows=len(rows), cols=len(rows[0]))
                tb.style = "Table Grid"
                for i, r in enumerate(rows):
                    for j, c in enumerate(r):
                        para = tb.cell(i, j).paragraphs[0]
                        _set_font(para.add_run(str(c)))
            elif t in ("h1", "h2", "h3"):
                lvl = int(t[1])
                heading = doc.add_heading(b.get("text", ""), level=lvl)
                if heading.runs:
                    _set_font(heading.runs[0])
            else:  # 默认当正文 p
                para = doc.add_paragraph()
                _set_font(para.add_run(b.get("text", "")))

        # —— 原子写: 先写 .tmp 再覆盖, 断电也不会写坏原文件 ——
        d = os.path.dirname(filepath)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        tmp = filepath + ".tmp"
        doc.save(tmp)
        os.replace(tmp, filepath)
        return f"Word '{filepath}' 写入成功（{len(blocks)} 个块）。"
    except Exception as e:
        return f"写Word失败: {str(e)}"


def write_file(filepath: str, content: str):
    filepath = filepath.replace('\\', '/').strip().strip('"').strip("'")
    _safe, _reason = is_path_safe(filepath)
    if not _safe:
        log_hit("write_denied", _reason, filepath)
        return "[已拒绝] 写入未通过路径安全校验：%s。请换到桌面/工作空间等安全路径。" % _reason
    message_queue.put(("AI_CHUNK", f"\n[📝 路夜正在写入文件: {filepath}...]"))
    try:
        parent_dir = os.path.dirname(os.path.abspath(filepath))
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        return f"文件 '{filepath}' 写入成功。"
    except Exception as e:
        return f"写入文件失败: {str(e)}"

# ---------- 补丁⑧: 命令执行工具(run_command) ----------
# 白名单命令直接跑；危险命令/白名单外的命令 必须弹窗让老大确认；硬黑名单直接拒
_RUN_ALLOW_FIRST = {
    "python", "python3", "py", "pip", "pip3", "pytest", "ruff", "mypy", "black",
    "git", "node", "npm", "npx", "pnpm", "yarn", "java", "javac",
    "gradle", "gradlew", "mvn", "conda",
    "dir", "type", "echo", "where", "whoami", "ver", "tree", "findstr",
    "chcp", "set", "cd", "cls", "more", "find", "sort",
    # ==== 外部工具白名单（AGENTS.md：接入新库/工具时，同步在此登记其可执行名）====
    "officecli",  # OfficeCLI：docx/xlsx/pptx 细读、元素级写、validate、view issues/screenshot
    # ppt-master 脚本（经 python 调用本就放行；此处兼容直接执行 .py 的场景）
    "source_to_md", "project_manager", "icon_sync", "analyze_images", "image_gen",
    "svg_quality_checker", "pptx_to_svg", "svg_to_pptx", "apply_template",
    "attribution_guard", "pptx_delivery_check", "batch_validate",
    "template_preview_pptx", "update_repo",
}

_RUN_DANGER = re.compile(
    r"\bdel\s|\berase\s|\brd\s+/|\brmdir\s|\brm\s+-|\bformat\s+[a-z]|\bdiskpart\b|"
    r"\bshutdown\b|\btaskkill\b|\breg\s+(?:add|delete)\b|\bsc\s+delete\b|"
    r"\bnet\s+(?:user|localgroup)\b|git\s+push|git\s+reset\s+--hard|git\s+clean|"
    r"git\s+branch\s+-D|git\s+checkout\s+--\s|pip\s+uninstall|--force-reinstall|"
    r"(?:^|[&|;]\s*)move\s|(?:^|[&|;]\s*)ren\s|\brename\s|\bcopy\s+/y|takeown|icacls|\brunas\b|"
    r"powershell|pwsh|\bwmic\b|regedit|msconfig|\bcurl\b|\bwget\b|\biex\b|"
    r"invoke-expression|start-process|rmtree|shutil\.|os\.remove|os\.unlink|>\s*\S",
    re.I,
)

_RUN_DENY = re.compile(
    r"format\s+[a-z]:|\brd\s+/s\s+/q\s+[a-z]:|\bdel\s+/[fsq]+\s+[a-z]:\\|"
    r"del\s+/[fsq]+\s+c:\\|windows\\system32|diskpart\s|bcdedit|vssadmin\s+delete|"
    r"cipher\s+/w|mkfs|dd\s+if=",
    re.I,
)


def _cmd_decode(raw):
    # Windows 中文命令行输出多是 GBK，先试探再兜底
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


def _cmd_trunc(text, limit=4000):
    # 头尾各留一半，编译日志/报错通常在尾巴上
    if not text or len(text) <= limit:
        return text or ""
    half = limit // 2
    return text[:half] + "\n...（中间省略 %d 字）...\n" % (len(text) - limit) + text[-half:]


def _cmd_risk(cmd):
    # 白名单内 -> None（自动跑）；否则返回风险说明（必须先确认）
    m = _RUN_DANGER.search(cmd)
    if m:
        return "命中危险关键字: " + m.group(0).strip()
    for seg in re.split(r"&&|\|\||;|\|", cmd):
        seg = seg.strip()
        if not seg:
            continue
        first = seg.split()[0].strip("\"'").lower()
        first = os.path.basename(first)
        for ext in (".exe", ".bat", ".cmd", ".ps1", ".py"):
            if first.endswith(ext):
                first = first[:-len(ext)]
                break
        if first not in _RUN_ALLOW_FIRST:
            return "不在白名单里的命令: " + first
    return None


def _ask_confirm(cmd, reason, why="", detail="", wait_sec=180):
    # 弹窗归主线程，后台线程在这里等结果；等不到就当拒绝
    # why: 路夜的意图与目的（老大拍板前最想看的那一行）
    # detail: 工作目录 / 超时等附加信息
    ev = threading.Event()
    box = {"ok": False}
    message_queue.put(("CMD_CONFIRM", (cmd, reason, why, detail, box, ev)))
    if ev.wait(timeout=wait_sec):
        return bool(box.get("ok"))
    return False


def run_command(cmd, timeout=60, cwd=None, why=""):
    # 执行本地命令：白名单自动跑 / 危险命令先让老大点确认 / 硬黑名单直接拒
    import subprocess
    cmd = (cmd or "").strip()
    if not cmd:
        return "[拒绝] 命令为空。"
    if "\x00" in cmd or len(cmd) > 2000:
        return "[拒绝] 命令含非法字符或过长。"

    deny = _RUN_DENY.search(cmd)
    if deny:
        log_hit("cmd_denied", deny.group(0), cmd)
        return "[已拒绝] 命中硬黑名单(%s)，危险度太高，不执行也不询问。" % deny.group(0)

    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = 60
    timeout = max(5, min(timeout, 300))

    workdir = cwd or os.path.dirname(os.path.abspath(__file__))
    workdir = str(workdir).strip().strip("\"").strip("'")
    if not os.path.isdir(workdir):
        workdir = os.path.dirname(os.path.abspath(__file__))

    why = (why or "").strip().replace("\r", " ").replace("\n", " ")
    if len(why) > 300:
        why = why[:300] + "…"

    risk = _cmd_risk(cmd)
    if risk:
        if not why:
            # 老大要先看「意图与目的」才拍板；没写就打回，不让老大盲批
            log_hit("cmd_no_why", risk, cmd)
            return ("[系统] 这条命令属于「%s」，需要老大在弹窗里点「是」才能跑；"
                    "但你没带 why（意图与目的），等于让老大盲批，已打回、未执行。"
                    "请带上 why 参数重新调用：一句话说清为什么跑它、要达成什么结果。" % risk)
        log_hit("cmd_confirm", risk, cmd)
        log_hit("cmd_why", why, cmd)
        message_queue.put(("AI_CHUNK", "\n[⚠️ 危险命令，等老大拍板] %s\n[🎯 目的] %s\n" % (cmd, why)))
        _ok = _ask_confirm(cmd, risk, why, "工作目录: %s\n最长运行: %d 秒" % (workdir, timeout))
        log_hit("cmd_approved" if _ok else "cmd_rejected", why, cmd)
        if not _ok:
            return "[已取消] 老大没批准这条命令，未执行。"
    else:
        message_queue.put(("AI_CHUNK", "\n[🖥️ 路夜正在执行: %s]" % cmd))

    try:
        proc = subprocess.run(cmd, shell=True, cwd=workdir,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout)
        out = _cmd_decode(proc.stdout or b"")
        code = proc.returncode
    except subprocess.TimeoutExpired as e:
        out = _cmd_decode(e.stdout) if isinstance(e.stdout, (bytes, bytearray)) else ""
        return "[超时] 超过 %d 秒未结束，已强制终止。\n--- 已捕获输出 ---\n%s" % (timeout, _cmd_trunc(out))
    except Exception as e:
        return "[执行失败] %s" % e

    body = _cmd_trunc(out)
    return "[退出码 %d] 工作目录: %s\n%s" % (code, workdir, body if body.strip() else "（命令没有输出）")


SKILLS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills", "skills")


def load_skills_catalog():
    """扫 skills/**/SKILL.md，抓 frontmatter 的 name/description。
    每轮现扫，改了 SKILL.md 下一轮立刻生效。"""
    catalog = []
    if not os.path.isdir(SKILLS_ROOT):
        return catalog
    for root, _dirs, files in os.walk(SKILLS_ROOT):
        if "SKILL.md" not in files:
            continue
        p = os.path.join(root, "SKILL.md")
        try:
            with open(p, "r", encoding="utf-8") as f:
                head = f.read(2000)
        except Exception:
            continue
        m = re.match(r"^---\s*\n(.*?)\n---", head, re.S)
        if not m:
            continue
        fm = m.group(1)
        m2 = re.search(r"^name:\s*(.+)$", fm, re.M)
        name = m2.group(1).strip() if m2 else os.path.basename(root)
        m3 = re.search(r"^description:\s*(.+)$", fm, re.M)
        desc = m3.group(1).strip().strip('"').strip("'").strip() if m3 else ""
        if len(desc) > 120:
            first = desc.split(". ")[0]
            desc = (first if len(first) <= 120 else desc[:120]).rstrip() + "…"
        manual = "disable-model-invocation: true" in fm
        catalog.append((name, desc, p.replace("\\", "/"), manual))
    return sorted(catalog, key=lambda x: x[0])


def build_skills_prompt():
    """拼出「技能目录」注入给模型。只给名字+一句话+路径，不给正文。"""
    cat = load_skills_catalog()
    if not cat:
        return ""
    lines = ["【可用技能清单】需要哪个就用 read_skill 读它的全文，不要凭名字瞎猜。"]
    for name, desc, path, manual in cat:
        tag = " [仅老大手动调用]" if manual else ""
        lines.append(f"- /{name}{tag}: {desc}\n  路径: {path}")
    return "\n".join(lines)


def read_skill(name: str):
    """按名字读取 SKILL.md 全文。"""
    name = str(name).strip().lstrip("/")
    for nm, _d, path, _m in load_skills_catalog():
        if nm == name:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                return f"读技能失败: {e}"
    return f"没有这个技能: {name}"

def dispatch_tool(name: str, args: dict) -> str:
    """统一执行入口，带异常兜底，避免一个工具炸了整轮对话就废。"""
    if not isinstance(args, dict):
        args = {}
    try:
        if name == "web_search":
            return web_search(args.get("query", ""))
        elif name == "list_directory":
            return list_directory(args.get("path", "."))
        elif name == "read_file":
            try:
                s = int(args.get("start_line", 1) or 1)
            except (TypeError, ValueError):
                s = 1
            try:
                e = int(args.get("end_line", 200) or 200)
            except (TypeError, ValueError):
                e = 200
            return read_file(args.get("filepath", ""), s, e)
        elif name == "write_file":
            return write_file(args.get("filepath", ""), args.get("content", ""))
        elif name == "read_word":
            try:
                s = int(args.get("start_line", 1) or 1)
            except (TypeError, ValueError):
                s = 1
            e = args.get("end_line")
            try:
                e = int(e) if e else None
            except (TypeError, ValueError):
                e = None
            return read_word(args.get("filepath", ""), s, e)
        elif name == "write_word":
            return write_word(args.get("filepath", ""), args.get("blocks", []), args.get("mode", "new"))
        elif name == "read_skill":
            return read_skill(args.get("name", ""))
        elif name == "run_command":
            try:
                _to = int(args.get("timeout", 60) or 60)
            except (TypeError, ValueError):
                _to = 60
            _why = args.get("why") or args.get("reason") or args.get("purpose") or ""
            return run_command(args.get("cmd", ""), _to, args.get("cwd"), _why)
        return f"未知工具: {name}"
    except Exception as e:
        return f"工具执行异常: {e}"

def show_windows_toast(title, message):
    try:
        toast = Notification(app_id="赛博小弟", title=title, msg=message, duration="short")
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception:
        pass

# ---------- 补丁⑪: 开机问候联动（启动时蹦一句活人感问候） ----------
def _boot_greeting_worker(app):
    """后台线程：让 boot_greet 生成一句问候（受每日上限控制），再交主线程弹气泡。"""
    text = None
    try:
        import boot_greet
        text = boot_greet.build_greeting()
    except Exception as e:
        log_hit("greet_fail", str(e), "")
        return
    if not text:
        return
    try:
        app.root.after(0, lambda: app.pop_boot_greeting(text))
    except Exception:
        pass


def monitor_memory(threshold_mb=800):
    while True:
        try:
            for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
                if proc.info['pid'] in alerted_pids:
                    continue
                mem_mb = proc.info['memory_info'].rss / (1024 * 1024)
                if mem_mb > threshold_mb:
                    alerted_pids.add(proc.info['pid'])
                    proc_name = proc.info['name']
                    message_queue.put(("ALERT", f"⚠️ 检测到进程 {proc_name} 内存高达 {mem_mb:.0f} MB！建议检查！"))
            time.sleep(15)
        except Exception:
            time.sleep(15)

# ==================== 核心：多轮工具调用 + 收窄版防偷懒拦截 ====================
TOOLS = [
    {"type": "function", "function": {"name": "web_search", "description": "联网搜索", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "list_directory", "description": "扫描文件夹", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "read_file", "description": "读取文件的指定行数。每次最多读400行。如果文件太长，请分多次读取。", "parameters": {"type": "object", "properties": {"filepath": {"type": "string", "description": "文件的绝对路径"}, "start_line": {"type": "integer", "description": "起始行号，默认为1"}, "end_line": {"type": "integer", "description": "结束行号，默认为200"}}, "required": ["filepath"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "将内容写入本地文件。如果文件不存在则创建，存在则覆盖。", "parameters": {"type": "object", "properties": {"filepath": {"type": "string", "description": "文件的绝对路径"}, "content": {"type": "string", "description": "要写入文件的完整文本内容"}}, "required": ["filepath", "content"]}}},
    {"type": "function", "function": {"name": "read_word", "description": "读取Word文档(.docx)的文本内容，支持分块。如果文件太长，请分多次读取。", "parameters": {"type": "object", "properties": {"filepath": {"type": "string", "description": "docx文件的绝对路径"}, "start_line": {"type": "integer", "description": "起始行号，默认为1"}, "end_line": {"type": "integer", "description": "结束行号，默认为读到结尾"}}, "required": ["filepath"]}}},
    {"type": "function", "function": {"name": "write_word","description": "创建或追加 Word(.docx)。blocks 是分块列表, 每块形如 {\"type\":\"h1|h2|p\",\"text\":\"...\"} 或 {\"type\":\"table\",\"rows\":[[...],[...]]}。mode 可选 new(新建)/append(追加)。","parameters": {"type": "object","properties": {"filepath": {"type": "string", "description": "docx 绝对路径"},"blocks": {"type": "array", "items": {"type": "object"}, "description": "内容分块列表"},"mode": {"type": "string", "enum": ["new", "append"], "description": "默认 new"}},"required": ["filepath", "blocks"]}}},
    {"type": "function","function": {"name": "read_skill","description": "读取指定技能(SKILL.md)的完整正文。先看系统提示里的技能清单，需要哪个就读哪个。","parameters": {"type": "object","properties": {"name": {"type": "string","description": "技能名，如 grill-with-docs / tdd / diagnosing-bugs（不带斜杠）",}},"required": ["name"],},},},

    {"type": "function", "function": {"name": "run_command", "description": "在本机执行一条命令行(Windows cmd)。白名单内的普通命令会直接执行；危险命令或不在白名单里的命令，必须先由老大在弹出的确认框里点「是」，否则不会执行。调用时必须填 why：一句话说清为什么跑它、要达成什么结果，老大会在确认框里先看这句再决定批不批；危险命令没写 why 会被直接打回。输出会截断到约4000字。", "parameters": {"type": "object", "properties": {"cmd": {"type": "string", "description": "完整命令行，例如 python scripts\\attribution_guard.py"}, "why": {"type": "string", "description": "必填。一句话说明意图与目的：为什么跑它、想拿到什么结果。会原样显示在确认框里给老大看"}, "timeout": {"type": "integer", "description": "超时秒数，默认60，最大300"}, "cwd": {"type": "string", "description": "工作目录，默认 desktop_bug.py 所在目录"}}, "required": ["cmd", "why"]}}},
]

# 只保留"明确承诺去执行动作"的说法；"确认一下/检查一下/稍等"这类日常词已剔除
ACTION_PROMISES = ["我这就去读", "我这就去看", "我马上读", "我现在去读", "我读一下这个文件",
                   "我这就打开", "正在读取", "我这就写", "我马上写", "我这就扫描"]

# ---------- 补丁① 常量: 工具调用文本泄漏哨兵 ----------
_HALF_BAR = chr(0xFF5C)          # 全角竖线
_FULL_BAR = _HALF_BAR * 2
_DL = "D" + "S" + "M" + "L"
_INV = "in" + "voke"
_PRM = "para" + "meter"
# ---------- 补丁④：真结构判据（只认真正的调用结构，不认“字样”） ----------
_STRUCT_PAT = re.compile(
    r"<[^<>]{0,24}?(?:" + _INV + "|" + _PRM + r")\s+name\s*="
    r"|</[^<>]{0,24}?(?:" + _INV + "|" + _PRM + r")\s*>"
    r"|" + _HALF_BAR + r"{1,2}\s*" + _DL + r"\s*" + _HALF_BAR + r"{1,2}"
    r"|" + "ant" + r"ml:(?:" + _INV + "|" + _PRM + r")",
    re.I,
)

_HITS_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentinel_hits.log")

def log_hit(where, frag, ctx):
    try:
        with open(_HITS_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s frag=%r tail=%r\n" % (
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                where, frag[:120], (ctx or "")[-160:]))
    except Exception:
        pass

def _looks_like_tool_text(s):
    return bool(_STRUCT_PAT.search(s or ""))

def _sanitize_tool_text(text):
    if not text:
        return ""
    return chr(10).join(ln for ln in text.splitlines() if not _STRUCT_PAT.search(ln)).strip()


# ---------- 公共：文本形式的工具调用解析（非流式/流式共用，治本） ----------
_B_STRUCT_LOOSE = re.compile(
    r'<[^>]*invoke\s+name\s*=|[｜|]DSML[｜|]|parameter\s+name\s*=|antml:invoke'
)


def parse_dsml_to_toolcall(text):
    """从文本里解析 DSML/伪标签工具调用，返回 (name, args)；解析不出返回 (None, None)。"""
    if not text or not _B_STRUCT_LOOSE.search(text):
        return None, None
    tool_to_call = None
    tool_args = {}
    name_match = re.search(r'invoke\s+name="(.*?)"', text) or re.search(r'name="(.*?)"', text)
    if name_match:
        tool_to_call = name_match.group(1)
    param_matches = re.findall(r'parameter\s+name="([^"]+)"[^>]*>(.*?)</', text, re.S)
    for p_name, p_value in param_matches:
        p_value = p_value.strip()
        if p_name in ("path", "file_path", "filename", "file"):
            p_name = "filepath"
        if p_name in ("start_line", "end_line", "start_index", "max_items", "start_from_page", "start_number"):
            try:
                p_value = int(p_value)
            except (ValueError, TypeError):
                p_value = 0
        tool_args[p_name] = p_value
    if tool_to_call == "write_file" and "content" not in tool_args:
        content_match = re.search(r'name="content"[^>]*>(.*?)</', text, re.S)
        if content_match:
            tool_args["content"] = content_match.group(1).strip()
    if tool_to_call and tool_args:
        return tool_to_call, tool_args
    return None, None


def stream_final(temp_messages, messages_context):
    """没有工具要调用了，走流式输出收尾。
    返回三元组 (text, toolcall, retry)：
      text     —— 可入库的干净正文；剥空时会回捞已经流给老大看的安全内容
      toolcall —— 命中且能解析出完整调用时给出 (name, args)，交上层去执行
      retry    —— 整条都是无效标记、且回捞失败时置 True，交上层让它重答
    """
    full_answer = ""
    safe_all = ""
    pending = ""
    suppress_left = 0
    shown = ""                     # P7: 已经推上屏的正文，收尾时用来补发缺口
    finish_reason = None           # P7: 撞长度上限时 finish_reason == "length"
    try:
        resp = client.chat.completions.create(
            model="deepseek-flash", messages=temp_messages, stream=True,
            max_tokens=OUTPUT_MAX_TOKENS)
    except Exception:
        # 上游不认 max_tokens 就退回默认，绝不让一次参数问题毁掉整轮
        resp = client.chat.completions.create(
            model="deepseek-flash", messages=temp_messages, stream=True)
    for chunk in resp:
        if INTERRUPT_EVENT.is_set():          # P2: 逐块查，命中立刻掐连接
            try:
                resp.close()
            except Exception:
                pass
            raise Interrupted()
        if not chunk.choices:
            continue
        _fr = chunk.choices[0].finish_reason
        if _fr == "length" or (finish_reason is None and _fr):
            finish_reason = _fr          # P7: length 优先级最高，不许被后面的 stop 覆盖
        delta = chunk.choices[0].delta.content
        if not delta:
            continue
        full_answer += delta
        pending += delta

        m = _STRUCT_PAT.search(pending)
        if m:
            frag = m.group(0)
            log_hit("stream", frag, full_answer)
            suppress_left = 400           # P7: 闸门只压结构本身，长回复不许被吞掉
            pending = pending[:m.start()] + pending[m.end():]

        if suppress_left > 0:
            # 闸门期间一律吞；成块结构里通常没有空行，遇空行才开闸
            if chr(10) * 2 in pending:
                pending = pending.split(chr(10) * 2, 1)[1]
                suppress_left = 0
            else:
                suppress_left -= len(pending)
                pending = ""

        cut = len(pending)
        while cut > 0 and pending[cut - 1] in ("<", "/", ">", '"', _HALF_BAR, "|"):
            cut -= 1
        safe, pending = pending[:cut], pending[cut:]
        if safe:
            safe_all += safe
            shown += safe              # P7
            message_queue.put(("AI_CHUNK", safe))

    if pending and suppress_left <= 0:
        safe_all += pending
        shown += pending           # P7
        message_queue.put(("AI_CHUNK", pending))

    clean = _sanitize_tool_text(full_answer)

    # P7-a: 撞到长度上限就明说，别让老大以为是路夜自己话说一半
    if finish_reason == "length":
        message_queue.put(("AI_CHUNK",
            chr(10) + "[注意] 本轮输出撞到长度上限被截断；已说到的地方都进了记忆，"
            "要剩下的就让路夜「接着写」。" + chr(10)))

    # P7-b: 屏幕比正文少 -> 把没上屏的尾巴补发出来（治「看着断了一半」）
    if clean:
        if clean.startswith(shown):
            _tail = clean[len(shown):]
            if _tail.strip():
                message_queue.put(("AI_CHUNK", _tail))
        elif shown.strip():
            message_queue.put(("AI_CHUNK",
                chr(10) + "[注意] 本轮有内容被结构清理挡在屏外，完整文本已写入记忆。" + chr(10)))

    if clean:
        return clean, None, False

    if full_answer.strip():
        # ① 先回捞：把这些已经推给老大的安全正文认下来（修「屏幕上有字、却报空响应」）
        salvaged = _sanitize_tool_text(safe_all)
        if salvaged:
            log_hit("stream_salvaged", "recovered", full_answer)
            return salvaged, None, False
        # ② 再看能不能当工具调用救回来
        tc = parse_dsml_to_toolcall(full_answer)
        if tc[0]:
            log_hit("stream_toolcall_recovered", tc[0], full_answer)
            return "", tc, False
        # ③ 都不行，交上层重答，别丢罐头台词
        log_hit("stream_all_stripped", "stripped", full_answer)
        return "", None, True

    log_hit("stream_empty", "", "")
    fallback = "（模型这轮返回了空响应，路夜没收到任何内容。）"
    message_queue.put(("AI_CHUNK", fallback + chr(10)))
    return fallback, None, False
def ai_chat_worker(user_input, messages_context):
    check_and_fold_history(messages_context)

    messages_context.append({"role": "user", "content": user_input})

    now_time = datetime.datetime.now().strftime("%Y年%m月%d日 %H:%M:%S 星期%w")
    temp_messages = messages_context.copy()

    # ---------- v1.2 分层注入：L1 锚点每轮强制注入；L2 议题命中才注入 ----------
    if memory is not None:
        try:
            _anchors = memory.build_anchors_prompt()
            if _anchors:
                temp_messages.insert(1, {"role": "system", "content": _anchors})
            _l2 = memory.build_long_term_prompt(user_input)
            if _l2:
                temp_messages.append({"role": "system", "content": _l2})
        except Exception as e:
            log_hit("tier_inject_fail", str(e), "")
    temp_messages.append({"role": "system", "content": f"【当前系统实时时间】: {now_time}。当用户询问时间、日期或需要推算时，请严格以此为准，不要瞎编。"})
    skills_prompt = build_skills_prompt()
    if skills_prompt:
        temp_messages.append({"role": "system", "content": skills_prompt})
    temp_messages.append({"role": "system", "content": (
        "【输出格式铁律】需要调用工具时，必须通过 API 原生 tool_calls 字段发起；"
        "严禁在正文里输出任何形式的工具调用文本或伪标签。"
        "正文只写给老大看的自然语言。若无法调用工具，就直接用自然语言说明，"
        "不要伪造调用文本。"
    )})

    temp_messages.append({"role": "system", "content": (
        "【run_command 铁律】每次调用 run_command 都必须带 why 字段："
        "一句话说清「为什么跑它、要达成什么结果」。老大会在确认框里先看这句再决定批不批；"
        "需要确认的命令如果没写 why，会被系统直接打回、不执行，你必须补上 why 重新调用。"
    )})
    MAX_TOOL_ROUNDS = 30
    OUTPUT_MAX_TOKENS = 8000   # P7: 显式放宽输出上限，撞限要能报出来
    called_signatures = {}   # 防死循环：同样的调用重复超过 2 次就不再强推
    full_answer = ""

    # ==================== P0: 工具结果留痕 ====================
    # 工具结果原来只进 temp_messages、不进 history；这里在 assistant 正文尾部挂一份精简留痕，
    # 不改 schema、不动 memory.py。
    # 坑：留痕行必须以「[工具留痕]」起头、不能带 emoji —— memory._NOISE_LINE 会把
    #     emoji 方括号开头的整行当噪声剥掉。
    tool_log = []

    def _brief(_v, _n=120):
        _s = _v if isinstance(_v, str) else repr(_v)
        _s = " ".join(str(_s).split())
        return _s[:_n] + ("…" if len(_s) > _n else "")

    def _run_tool(_name, _args):
        """dispatch_tool 的唯一入口：照原样执行，顺手记一条精简留痕。"""
        _r = dispatch_tool(_name, _args)
        try:
            tool_log.append({
                "name": _name,
                "args": _brief(json.dumps(_args, ensure_ascii=False), 80),
                "res": _brief(_r, 120),
            })
        except Exception:
            pass
        return _r

    def _format_tool_trail(_log, max_items=12, max_chars=1200):
        """把留痕压成一段挂在正文尾部；超过上限只留最近若干条。"""
        if not _log:
            return ""
        items = _log[-max_items:]
        lines = ["\n\n[工具留痕] 本轮执行了 %d 次工具调用（只列最近 %d 条）："
                 % (len(_log), len(items))]
        for _it in items:
            lines.append("· %s(%s) -> %s" % (_it["name"], _it["args"], _it["res"]))
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        return text

    try:
        for round_num in range(MAX_TOOL_ROUNDS):
            if INTERRUPT_EVENT.is_set():          # P2: 覆盖工具轮，停了就别再跑工具
                raise Interrupted()
            if round_num == MAX_TOOL_ROUNDS - 1:
                temp_messages.append({"role": "system", "content": (
                    "【工具已用尽】本轮不能再调用任何工具。请只用自然语言总结已有信息，"
                    "禁止输出任何调用标记、伪标签或残片。"
                )})
                _text, _tc, _retry = stream_final(temp_messages, messages_context)
                full_answer = _text or ""
                break

            response = client.chat.completions.create(
                model="deepseek-flash", messages=temp_messages, tools=TOOLS, stream=False
            )
            msg = response.choices[0].message

            if os.environ.get("DEBUG_TOOL_CALL") == "1":
                print(f"[DEBUG] round={round_num}")
                print(f"[DEBUG] tool_calls={msg.tool_calls}")
                print(f"[DEBUG] content={repr(msg.content)[:500]}")

            # ---------- A. 标准格式：可能一次性返回多个工具调用 ----------
            if msg.tool_calls:
                # 关键修复：把 assistant 这次的调用请求原样回填，模型才知道自己刚发过请求
                temp_messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments or "{}",
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                })

                for tc in msg.tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    sig = f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}"
                    called_signatures[sig] = called_signatures.get(sig, 0) + 1

                    if called_signatures[sig] > 2:
                        result = f"[系统] 你已重复调用同一工具 {called_signatures[sig]} 次且参数完全相同，请停止重复并直接给出结论或换一种做法。"
                    else:
                        result = _run_tool(name, args)   # P0: 统一走留痕入口

                    # 关键修复：标准成对回填 role="tool" + tool_call_id
                    temp_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                continue

                        # ---------- B. 兼容 DSML 文本格式（没有 tool_call_id，走 user 兜底） ----------
            content_text = msg.content if msg.content else ""
            if content_text and _B_STRUCT_LOOSE.search(content_text):
                tool_to_call, tool_args = parse_dsml_to_toolcall(content_text)
                if tool_to_call and tool_args:
                    sig = f"{tool_to_call}:{json.dumps(tool_args, ensure_ascii=False, sort_keys=True)}"
                    called_signatures[sig] = called_signatures.get(sig, 0) + 1
                    if called_signatures[sig] > 2:
                        result = f"[系统] 重复调用已被拦截（第 {called_signatures[sig]} 次），请直接给结论。"
                    else:
                        result = _run_tool(tool_to_call, tool_args)   # P0: 统一走留痕入口
                    temp_messages.append({"role": "user", "content": "【工具执行结果】: " + result + chr(10) * 2 + "请根据以上结果继续。如果任务未完成（比如文件未读完），请继续调用工具。"})
                    continue
                else:
                    temp_messages.append({"role": "user", "content": "[系统] 上一条消息含无效标记，已忽略。请直接继续。"})
                    continue
            # ---------- C. 收窄版拦截器：只拦"嘴上说要动手" ----------
            if (any(kw in content_text for kw in ACTION_PROMISES)
                    and not looks_like_question(content_text)
                    and len(content_text) < 200
                    and round_num < MAX_TOOL_ROUNDS - 2):
                # P7: 被拦下的这句也得让人看见，否则屏幕上会出现无解释的空白
                message_queue.put(("AI_CHUNK", content_text + chr(10)))
                temp_messages.append({"role": "assistant", "content": content_text})
                temp_messages.append({"role": "user", "content": "你刚才说要动手，但没有调用任何工具。请不要只说空话，立刻调用 read_file 或相关工具去执行。"})
                continue

                        # ---------- D. 正常聊天，流式输出 ----------
            # 如果内容里还残留 DSML 标记，说明是解析失败的残渣，不要直接输出
            if _STRUCT_PAT.search(content_text):
                clean_text = _sanitize_tool_text(content_text)
                if clean_text:
                    full_answer += clean_text + "\n"
                    message_queue.put(("AI_CHUNK", clean_text + "\n"))
                else:
                    # 补丁⑤: 剥完什么都不剩 -> 不静默空白, 让模型重答
                    log_hit("caseD_empty", "stripped", content_text)
                    temp_messages.append({"role": "user", "content": "[系统] 你刚输出的内容被判定为无效工具标记，已丢弃。请直接用自然语言重新回答，不要输出任何标记。"})
                    continue
                break

            _text, _tc, _retry = stream_final(temp_messages, messages_context)
            if _tc:
                _name, _args = _tc
                _sig = f"{_name}:{json.dumps(_args, ensure_ascii=False, sort_keys=True)}"
                called_signatures[_sig] = called_signatures.get(_sig, 0) + 1
                if called_signatures[_sig] > 2:
                    _result = f"[系统] 重复调用已被拦截（第 {called_signatures[_sig]} 次），请直接给结论。"
                else:
                    _result = _run_tool(_name, _args)   # P0: 统一走留痕入口
                temp_messages.append({"role": "user", "content": "【工具执行结果】: " + _result + chr(10) * 2 + "请根据以上结果继续。如果任务未完成，请继续调用工具。"})
                continue
            if _retry:
                log_hit("stream_retry", "retry", "")
                temp_messages.append({"role": "user", "content": "[系统] 上一条回答被判定为无效工具标记残渣，已丢弃。请直接用自然语言重新回答，不要输出任何标记。"})
                continue
            full_answer = _text or ""
            break

        _trail = _format_tool_trail(tool_log)   # P0: 工具留痕挂正文尾一起落盘
        if _trail:
            full_answer = (full_answer or "") + _trail
            message_queue.put(("AI_CHUNK", "\n[工具留痕] 本轮 %d 次工具调用的结果已写进记忆。\n"
                               % len(tool_log)))

        if full_answer:
            messages_context.append({"role": "assistant", "content": full_answer})
        else:
            # 补丁⑤: 整轮啥都没产出 -> 明确提示, 而不是静默空白
            log_hit("final_empty", "", "")
            msg = "（路夜这轮没憋出内容，模型可能返回了空响应。老大再问一句试试？）"
            message_queue.put(("AI_CHUNK", msg + "\n"))
            messages_context.append({"role": "assistant", "content": msg})

    except Interrupted:
        # P0 改了这里的判据：有工具留痕就不能撤回 user，否则留痕变孤儿；
        # 没留痕才按 P2 原逻辑撤回，避免 spawn 一条只有 user 的孤儿记录。
        _trail = _format_tool_trail(tool_log)
        if _trail:
            try:
                messages_context.append({"role": "assistant", "content": "[已中断]" + _trail})
            except Exception:
                pass
            message_queue.put(("AI_CHUNK", "\n[⏹ 已停止本轮生成；本轮工具结果已留痕。]\n"))
        else:
            try:
                if (messages_context
                        and messages_context[-1].get("role") == "user"
                        and messages_context[-1].get("content") == user_input):
                    messages_context.pop()
            except Exception:
                pass
            message_queue.put(("AI_CHUNK", "\n[⏹ 已停止本轮生成，后续不再继续。]\n"))
    except Exception as e:
        message_queue.put(("AI_CHUNK", f"\n[❌ 路夜大脑短路了: {str(e)}]\n"))
    finally:
        save_history(messages_context)
        message_queue.put(("AI_DONE", None))

# ==================== 主界面（GUI）模块 ====================
class DesktopBugApp:
    # ---- v1.2.1 热补丁：发送按钮 / 打字状态兜底参数 ----
    TYPING_IDLE_TIMEOUT = 300     # 秒：连续静默这么久才自动解锁（防长任务被误杀）
    TYPING_WATCH_INTERVAL = 5     # 秒：兜底巡检间隔

    # ---- P1 三点循环（等思考提示）----
    TYPING_FRAMES = [".", "..", "..."]
    TYPING_DOT_INTERVAL = 350     # 毫秒：一帧停留时间
    TYPING_PREFIX = "路夜: "      # 占位前缀，和正文的说话人保持一致

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("赛博蛀虫·修复版")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.9)

        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.window_width, self.window_height = 100, 100
        self.root.geometry(f"{self.window_width}x{self.window_height}+{sw - 150}+{sh - 200}")

        # ---- P5-B: 单窗口悬浮形象（圆形遮罩 + 透明穿透 + 置顶）----
        self.overlay_frames = []          # 已合成的 PhotoImage 帧
        self.overlay_durations = []       # 各帧时长（毫秒）
        self.overlay_frame_idx = 0
        self.image = None
        self._pet_shape = "circle"        # circle / round / none
        self._pet_size = self.PET_SIZE
        self._pet_bg_rgb = tuple(int(self.PET_KEY.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        self._pet_state = "idle"          # idle / sleep
        self._pet_dur = self.PET_FRAME_MS
        self._pet_pending = []
        self._pet_sleep_img = None        # P7: 打瞌睡静态图
        self._pet_wake_job = None         # P7: 穿透期间右键唤醒轮询句柄
        self._pet_rbtn_prev = False       # P7: 上一轮右键是否按下（取下降沿）
        self._pet_pass_through = False    # P7: 当前是否已整窗鼠标穿透
        self._pet_drag = None
        self._pet_tick_job = None
        self._pet_load_job = None
        self._pet_guard_job = None
        self._pet_single_job = None
        self._bubble_win = None
        self._heart_win = None            # P6: 摸头爱心浮窗
        self._heart_job = None
        self._tomato_win = None           # P6: 番茄钟胶囊
        self._tomato_label = None
        self._tomato_job = None
        self._tomato_left = 0
        self._closing = False

        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=self.PET_KEY)
        try:
            self.root.attributes("-transparentcolor", self.PET_KEY)
        except Exception:
            pass
        try:
            self.root.attributes("-alpha", 1.0)
        except Exception:
            pass

        self.label = tk.Label(self.root, bg=self.PET_KEY, bd=0,
                              highlightthickness=0, takefocus=0)
        self.label.pack(fill=tk.BOTH, expand=True)

        self.window_width = self.window_height = self._pet_size
        self.root.geometry("%dx%d+%d+%d" % (
            self._pet_size, self._pet_size,
            sw - self._pet_size - self.PET_MARGIN_R,
            sh - self._pet_size - self.PET_MARGIN_B))
        # 上次拖到哪，这次就停在哪（越界就回默认位）
        try:
            with open(self.PET_POS_FILE, "r", encoding="utf-8") as _f:
                _px, _py = [int(v) for v in _f.read().strip().split(",")]
            if -40 <= _px <= sw and -40 <= _py <= sh:
                self.root.geometry("+%d+%d" % (_px, _py))
        except Exception:
            pass

        # 逐帧合成（遮罩只算一次），首帧立刻显示
        self._load_pet_frames()

        # 单击 = 喊喵（延迟判定，给双击让路）；双击 = 打开对话
        self.label.bind("<ButtonPress-1>", self._pet_press)
        self.label.bind("<B1-Motion>", self._pet_drag_move)
        self.label.bind("<ButtonRelease-1>", self._pet_release)
        self.label.bind("<Double-Button-1>", self._pet_double_click)
        self.label.bind("<Button-3>", self.show_context_menu)

        self.start_x, self.start_y = 0, 0
        self.messages_context = load_history()

        self.chat_window = None
        self.chat_text_area = None
        self.input_entry = None
        self.send_button = None
        self._input_scroll = None      # 多行输入框的滚动条（8 行封顶后才浮出）
        self._input_rows = 3           # 输入框当前显示行数（3~8 自适应）
        self.is_typing = False
        self._typing_deadline = 0     # 打字超时时间戳（0 = 未计时）
        self._dots_job = None         # P1: 三点循环的 after 句柄
        self._dots_idx = 0            # P1: 当前帧序号

        # ---- 主动性：未读队列 + 聊天窗开合标志 ----
        self.pending_proactive = []
        self.chat_open_flag = False

        threading.Thread(target=monitor_memory, daemon=True).start()
        threading.Thread(target=_boot_greeting_worker, args=(self,), daemon=True).start()

        # ---- 主动性引擎：随机心跳 + 环境感知（导入失败不影响主程序） ----
        try:
            import proactive
            threading.Thread(
                target=proactive.worker,
                args=(self, lambda t, m: self.root.after(0, lambda: self.push_proactive(t, m))),
                daemon=True,
            ).start()
        except Exception:
            pass
        self.root.after(100, self.check_queue)
        self.root.after(self.TYPING_WATCH_INTERVAL * 1000, self._typing_watchdog)

        # ---- P5-B: 单窗口自带 Win+D 守护，不再拉起 pet_window.py 子进程 ----
        self._kill_legacy_pet()
        self._pet_guard_job = self.root.after(self.PET_GUARD_MS, self._pet_guard_tick)

    # ==================== P5-B: 单窗口悬浮形象（遮罩 + 穿透 + 置顶） ====================
    PET_SIZE = 120             # 显示边长（px），建议 96~140
    PET_KEY = "#FF00FF"        # 穿透底色（品红，图里不能有这个颜色）
    PET_FRAME_MS = 55          # 帧间隔 ≈18fps
    PET_GUARD_MS = 1200        # Win+D 守护轮询间隔
    PET_MARGIN_R = 40          # 距屏幕右边距
    PET_MARGIN_B = 80          # 距屏幕下边距
    PET_CLICK_MOVE_TOL = 5     # 位移小于该值视为点击
    PET_CLICK_TIME_TOL = 0.35  # 点击时长上限（秒）
    PET_DOUBLE_WAIT = 260      # 单击延迟（ms），给双击让路
    PET_BUBBLES = ("喵～", "在呢老大", "嗯？", "喵？")   # 单击随机气泡
    PET_ALPHA_SLEEP = 0.5
    PET_SLEEP_IMG_NAME = "bug.png"     # 打瞌睡静态图（assets/ 优先）
    PET_WAKE_POLL_MS = 60      # P7: 打瞌睡穿透后，右键唤醒轮询间隔（ms）
    PET_WAKE_VK_RBUTTON = 0x02 # P7: 虚拟键码——鼠标右键
    PET_POS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pet_pos.txt")

    # ---- P6: 摸头爱心 + 番茄钟 ----
    PET_HEART_CHAR = "\u2665"
    PET_HEART_MS = 45
    PET_HEART_STEPS = 16
    PET_HEART_RISE = 3
    PET_HEART_DRIFT = 1
    TOMATO_MINUTES = 25

    @staticmethod
    def _pet_mask(size, shape="circle"):
        """遮罩只算一次：圆 / 圆角 / 方形"""
        from PIL import Image, ImageDraw
        mask = Image.new("L", (size, size), 0)
        d = ImageDraw.Draw(mask)
        if shape == "round":
            d.rounded_rectangle((0, 0, size - 1, size - 1),
                                radius=max(6, size // 6), fill=255)
        elif shape == "none":
            d.rectangle((0, 0, size - 1, size - 1), fill=255)
        else:
            d.ellipse((0, 0, size - 1, size - 1), fill=255)
        return mask

    def _pet_media_path(self):
        """形象来源：assets/bug.gif > 根目录 bug.gif > 任意动图 > 任意图"""
        base = os.path.dirname(os.path.abspath(__file__))
        for p in (os.path.join(base, "assets", "bug.gif"),
                  os.path.join(base, "bug.gif")):
            if os.path.isfile(p):
                return p
        try:
            for ext in (".gif", ".webp", ".png"):
                for f in sorted(os.listdir(base)):
                    if f.lower().endswith(ext):
                        return os.path.join(base, f)
        except Exception:
            pass
        return None

    def _load_pet_frames(self):
        """读原始帧 -> 裁遮罩贴底色 -> 分批转 PhotoImage，避免启动卡住"""
        try:
            from PIL import Image
        except Exception:
            self._pet_text_fallback()
            return
        path = self._pet_media_path()
        if not path:
            self._pet_text_fallback()
            return

        size = self._pet_size
        mask = self._pet_mask(size, self._pet_shape)
        bg = self._pet_bg_rgb
        raw, dur = [], 0
        try:
            im = Image.open(path)
            n = int(getattr(im, "n_frames", 1))
            for i in range(n):
                im.seek(i)
                fr = im.convert("RGBA").resize((size, size), Image.LANCZOS)
                out = Image.new("RGB", (size, size), bg)
                out.paste(fr, (0, 0), mask)
                raw.append(out)
            dur = int(im.info.get("duration", 0) or 0)
        except Exception:
            raw = []

        if not raw:
            self._pet_text_fallback()
            return

        self._pet_dur = max(30, dur or self.PET_FRAME_MS)
        self._pet_pending = raw
        self.overlay_frames = []
        self.overlay_durations = []
        self._pet_load_sleep_frame()      # P7: 打瞌睡静态图
        self._pet_fill(0)

    def _pet_sleep_path(self):
        """打瞌睡静态图：assets/bug.png > 根目录 bug.png"""
        base = os.path.dirname(os.path.abspath(__file__))
        for p in (os.path.join(base, "assets", self.PET_SLEEP_IMG_NAME),
                  os.path.join(base, self.PET_SLEEP_IMG_NAME)):
            if os.path.isfile(p):
                return p
        return None

    def _pet_load_sleep_frame(self):
        """把打瞌睡静态图按同一套遮罩/底色处理；失败就留空，退回定格"""
        self._pet_sleep_img = None
        try:
            from PIL import Image, ImageTk
        except Exception:
            return
        path = self._pet_sleep_path()
        if not path:
            return
        try:
            size = self._pet_size
            fr = Image.open(path).convert("RGBA")
            w, h = fr.size
            ratio = min(size / float(w or 1), size / float(h or 1))
            nw, nh = max(1, int(w * ratio)), max(1, int(h * ratio))
            fr = fr.resize((nw, nh), Image.LANCZOS)
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            canvas.paste(fr, ((size - nw) // 2, (size - nh) // 2), fr)
            out = Image.new("RGB", (size, size), self._pet_bg_rgb)
            out.paste(canvas, (0, 0), self._pet_mask(size, self._pet_shape))
            self._pet_sleep_img = ImageTk.PhotoImage(out)
        except Exception:
            self._pet_sleep_img = None

    def _pet_fill(self, start, batch=10):
        """分批把 Pillow 图转成 PhotoImage（必须持有引用，否则被 GC 白屏）"""
        from PIL import ImageTk
        raw = self._pet_pending or []
        end = min(start + batch, len(raw))
        for i in range(start, end):
            try:
                self.overlay_frames.append(ImageTk.PhotoImage(raw[i]))
                self.overlay_durations.append(self._pet_dur)
            except Exception:
                pass
        if self.image is None and self.overlay_frames:
            self.image = self.overlay_frames[0]
            try:
                self.label.configure(image=self.image)
            except Exception:
                pass
        if end < len(raw):
            self._pet_load_job = self.root.after(15, self._pet_fill, end, batch)
        else:
            self._pet_pending = []
            self._pet_load_job = None
            self._start_pet_tick()

    def _pet_text_fallback(self):
        """没有图 / Pillow 缺失时的兜底方块，保证程序不空窗"""
        try:
            self.root.configure(bg="#1e90ff")
            self.label.configure(text="路夜", font=("微软雅黑", 12, "bold"),
                                 bg="#1e90ff", fg="white", width=8, height=4)
            self.root.geometry("%dx%d" % (self._pet_size, self._pet_size))
        except Exception:
            pass

    def _start_pet_tick(self):
        if self._pet_tick_job is None:
            self._pet_tick_job = self.root.after(self.PET_FRAME_MS, self._pet_tick)

    def _pet_tick(self):
        """idle 逐帧播，sleep 停在静态帧（400ms 一跳）"""
        self._pet_tick_job = None
        try:
            if (not self._closing and self._pet_state == "idle"
                    and len(self.overlay_frames) > 1):
                self.overlay_frame_idx = (self.overlay_frame_idx + 1) % len(self.overlay_frames)
                self.image = self.overlay_frames[self.overlay_frame_idx]
                self.label.configure(image=self.image)
        except Exception:
            pass
        try:
            self._pet_tick_job = self.root.after(
                self.PET_FRAME_MS if self._pet_state == "idle" else 400, self._pet_tick)
        except Exception:
            self._pet_tick_job = None

    def _pet_set_state(self, state):
        self._pet_state = "sleep" if state == "sleep" else "idle"
        try:
            self.root.attributes("-alpha",
                                 self.PET_ALPHA_SLEEP if self._pet_state == "sleep" else 1.0)
        except Exception:
            pass
        # P7: 打瞌睡换静态图；唤醒回到动图当前帧
        try:
            if self._pet_state == "sleep" and self._pet_sleep_img is not None:
                self.image = self._pet_sleep_img
                self.label.configure(image=self.image)
            elif self._pet_state == "idle" and self.overlay_frames:
                self.overlay_frame_idx = min(self.overlay_frame_idx,
                                             len(self.overlay_frames) - 1)
                self.image = self.overlay_frames[self.overlay_frame_idx]
                self.label.configure(image=self.image)
        except Exception:
            pass
        # P7: 打瞌睡 = 整窗鼠标穿透（左键全失效），只留右键轮询唤醒
        self._pet_apply_pass_through(self._pet_state == "sleep")

    # ------------------------------ P7: 打瞌睡穿透 / 右键唤醒
    def _pet_set_ex_style_transparent(self, on):
        """给顶层窗口加/去 WS_EX_TRANSPARENT。加上之后整个窗口不再接收任何鼠标事件。
        返回 True 表示样式已按预期生效（失败则调用方退回原行为）。"""
        if os.name != "nt":
            return False
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            h = self._pet_hwnd()
            if not h:
                return False
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_LAYERED = 0x00080000
            # 64 位必须显式声明入参，否则 HWND 会被当 c_int 截断
            u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            u.GetWindowLongW.restype = ctypes.c_long
            u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            u.SetWindowLongW.restype = ctypes.c_long
            u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_uint]
            cur = u.GetWindowLongW(h, GWL_EXSTYLE)
            if on:
                new = cur | WS_EX_TRANSPARENT | WS_EX_LAYERED
            else:
                new = cur & ~WS_EX_TRANSPARENT
            if new != cur:
                u.SetWindowLongW(h, GWL_EXSTYLE, new)
            # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED：让新样式立刻生效
            u.SetWindowPos(h, 0, 0, 0, 0, 0,
                           0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
            got = u.GetWindowLongW(h, GWL_EXSTYLE)
            return bool(got & WS_EX_TRANSPARENT) == bool(on)
        except Exception:
            return False

    def _pet_apply_pass_through(self, on):
        """切换整窗穿透；开启时同步启动右键唤醒轮询，关闭时停掉。"""
        ok = self._pet_set_ex_style_transparent(bool(on))
        self._pet_pass_through = bool(on) and ok
        self._pet_rbtn_prev = False
        if self._pet_pass_through:
            self._pet_wake_poll_start()
        else:
            self._pet_wake_poll_stop()
        return self._pet_pass_through

    def _pet_wake_poll_stop(self):
        job, self._pet_wake_job = self._pet_wake_job, None
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass

    def _pet_wake_poll_start(self):
        if self._pet_wake_job is None:
            try:
                self._pet_wake_job = self.root.after(
                    self.PET_WAKE_POLL_MS, self._pet_wake_poll)
            except Exception:
                self._pet_wake_job = None

    def _pet_cursor_inside(self, pad=6):
        """光标是否落在形象圆圈内（比矩形收窄 pad 像素，避免边缘误唤醒）"""
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            pt = wintypes.POINT()
            if not u.GetCursorPos(ctypes.byref(pt)):
                return False
        except Exception:
            return False
        try:
            x = self.root.winfo_x() + pad
            y = self.root.winfo_y() + pad
            side = float(self._pet_size - pad * 2)
            if side <= 0:
                return False
            cx, cy, r = x + side / 2.0, y + side / 2.0, side / 2.0
            return ((pt.x - cx) ** 2 + (pt.y - cy) ** 2) <= r * r
        except Exception:
            return False

    def _pet_wake_poll(self):
        """穿透之后唯一能叫醒路夜的路：右键按下沿 + 光标压在形象上。"""
        self._pet_wake_job = None
        if self._closing:
            return
        if self._pet_state != "sleep" or not self._pet_pass_through:
            return
        try:
            import ctypes
            down = bool(ctypes.windll.user32.GetAsyncKeyState(
                self.PET_WAKE_VK_RBUTTON) & 0x8000)
        except Exception:
            down = False
        try:
            if down and not self._pet_rbtn_prev and self._pet_cursor_inside():
                self._pet_set_state("idle")
                return
            self._pet_rbtn_prev = down
        except Exception:
            pass
        self._pet_wake_poll_start()

    def _pet_toggle_sleep(self):
        self._pet_set_state("idle" if self._pet_state == "sleep" else "sleep")

    def _pet_reload(self):
        for j in (self._pet_tick_job, self._pet_load_job):
            try:
                if j:
                    self.root.after_cancel(j)
            except Exception:
                pass
        self._pet_tick_job = None
        self._pet_load_job = None
        self.overlay_frames = []
        self.overlay_durations = []
        self.overlay_frame_idx = 0
        self.image = None
        self._pet_sleep_img = None
        try:
            self.label.configure(image="", text="")
        except Exception:
            pass
        self._load_pet_frames()

    # ---------------------------------------------------- 气泡
    def show_bubble(self, text, ms=1600):
        """悬浮窗上方的深色小气泡，到点自动消失；只保留一个，防叠罗汉"""
        try:
            if self._bubble_win and self._bubble_win.winfo_exists():
                self._bubble_win.destroy()
        except Exception:
            pass
        win = None
        try:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            tk.Label(win, text=text, bg="#2b2b2b", fg="#f0f0f0",
                     font=("微软雅黑", 9), padx=10, pady=6, bd=0,
                     wraplength=230, justify=tk.LEFT).pack()
            win.update_idletasks()
            bx = self.root.winfo_x() - max(0, (win.winfo_reqwidth() - self._pet_size) // 2)
            by = self.root.winfo_y() - win.winfo_reqheight() - 6
            win.geometry("+%d+%d" % (max(0, bx), max(0, by)))
            self._bubble_win = win
            win.after(ms, lambda: self._bubble_close(win))
        except Exception:
            return None
        return win

    def _bubble_close(self, win):
        try:
            if win and win.winfo_exists():
                win.destroy()
        except Exception:
            pass
        if self._bubble_win is win:
            self._bubble_win = None

    # ---------------------------------------------------- 交互
    def _pet_press(self, event):
        if self._pet_state == "sleep":      # P7: 打瞌睡左键全废
            return "break"
        self._pet_drag = {"x": event.x_root, "y": event.y_root,
                          "wx": self.root.winfo_x(), "wy": self.root.winfo_y(),
                          "t": time.time(), "moved": 0}

    def _pet_drag_move(self, event):
        if self._pet_state == "sleep":      # P7: 打瞌睡不拖拽
            return "break"
        d = self._pet_drag
        if not d:
            return
        dx = event.x_root - d["x"]
        dy = event.y_root - d["y"]
        d["moved"] = max(d["moved"], abs(dx) + abs(dy))
        try:
            self.root.geometry("+%d+%d" % (d["wx"] + dx, d["wy"] + dy))
        except Exception:
            pass

    def _pet_release(self, event):
        """轻点 -> 延迟 PET_DOUBLE_WAIT 再喊喵；双击会在中途取消它"""
        if self._pet_state == "sleep":      # P7: 打瞌睡不喊喵
            self._pet_drag = None
            return "break"
        d, self._pet_drag = self._pet_drag, None
        if not d:
            return
        if d["moved"] <= self.PET_CLICK_MOVE_TOL and (time.time() - d["t"]) <= self.PET_CLICK_TIME_TOL:
            if self._pet_single_job:
                try:
                    self.root.after_cancel(self._pet_single_job)
                except Exception:
                    pass
            self._pet_single_job = self.root.after(self.PET_DOUBLE_WAIT, self._pet_single_click)
        else:
            self._pet_sync_pos()

    def _pet_single_click(self):
        """单击 = 摸头：头顶飘一颗爱心 + 随机一句；双击会在中途取消它。"""
        self._pet_single_job = None
        self._pet_heart_pop()
        try:
            msg = random.choice(self.PET_BUBBLES)
        except Exception:
            msg = "喵～"
        self.show_bubble(msg)

    def _pet_double_click(self, event=None):
        # 双击第二下按下已登记 drag，这里清掉；否则松开时会再排一次单击任务，对话窗和「喵」齐飞
        self._pet_drag = None
        if self._pet_state == "sleep":      # P7: 打瞌睡不开对话
            return "break"
        if self._pet_single_job:
            try:
                self.root.after_cancel(self._pet_single_job)
            except Exception:
                pass
            self._pet_single_job = None
        self.open_chat_window(None)
        return "break"

    def _pet_sync_pos(self):
        """拖完记住位置，下次启动还在原地"""
        try:
            with open(self.PET_POS_FILE, "w", encoding="utf-8") as f:
                f.write("%d,%d" % (self.root.winfo_x(), self.root.winfo_y()))
        except Exception:
            pass

    # ---------------------------------------------------- P6: 摸头爱心 + 番茄钟
    def _pet_heart_pop(self):
        """摸头反馈：头顶飘一颗爱心，升空渐隐；不发声、不写记忆。"""
        self._pet_heart_clear()
        try:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg=self.PET_KEY)
            try:
                win.attributes("-transparentcolor", self.PET_KEY)
            except Exception:
                pass
            tk.Label(win, text=self.PET_HEART_CHAR, bg=self.PET_KEY, fg="#ff5c8a",
                     font=("Segoe UI Symbol", 16, "bold"), bd=0).pack()
            win.update_idletasks()
            bx = self.root.winfo_x() + max(0, (self._pet_size - win.winfo_reqwidth()) // 2)
            by = self.root.winfo_y() - win.winfo_reqheight()
            self._heart_win = win
            self._pet_heart_step(win, bx, by, 0)
        except Exception:
            self._pet_heart_clear()

    def _pet_heart_step(self, win, x, y, i):
        """一步一帧；关闭中、窗口没了、走完步数都就地收摊。"""
        self._heart_job = None
        if self._closing or not win.winfo_exists() or i >= self.PET_HEART_STEPS:
            self._pet_heart_clear()
            return
        try:
            win.geometry("+%d+%d" % (max(0, x + i * self.PET_HEART_DRIFT),
                                     max(0, y - i * self.PET_HEART_RISE)))
            win.attributes("-alpha", max(0.0, 1.0 - i / float(self.PET_HEART_STEPS)))
        except Exception:
            self._pet_heart_clear()
            return
        try:
            self._heart_job = self.root.after(
                self.PET_HEART_MS, lambda: self._pet_heart_step(win, x, y, i + 1))
        except Exception:
            self._heart_job = None

    def _pet_heart_clear(self):
        if self._heart_job:
            try:
                self.root.after_cancel(self._heart_job)
            except Exception:
                pass
        self._heart_job = None
        w, self._heart_win = self._heart_win, None
        try:
            if w and w.winfo_exists():
                w.destroy()
        except Exception:
            pass

    def _tomato_toggle(self):
        """右键菜单入口：没计时就开 25 分钟，正在计时就停。"""
        if self._tomato_job:
            self._tomato_stop("番茄钟已停下")
        else:
            self._tomato_start()

    def _tomato_start(self):
        self._tomato_left = self.TOMATO_MINUTES * 60
        self.show_bubble("番茄钟开始，%d 分钟" % self.TOMATO_MINUTES, 2200)
        self._tomato_tick()

    def _tomato_stop(self, tip=None):
        if self._tomato_job:
            try:
                self.root.after_cancel(self._tomato_job)
            except Exception:
                pass
        self._tomato_job = None
        self._tomato_left = 0
        self._tomato_chip_destroy()
        if tip:
            self.show_bubble(tip, 1800)

    def _tomato_tick(self):
        """每秒一格；胶囊每秒重画，到点弹气泡（不发声）。"""
        self._tomato_job = None
        if self._closing:
            return
        if self._tomato_left <= 0:
            self._tomato_done()
            return
        m, s = divmod(self._tomato_left, 60)
        self._tomato_chip("%d:%02d" % (m, s))
        self._tomato_left -= 1
        try:
            self._tomato_job = self.root.after(1000, self._tomato_tick)
        except Exception:
            self._tomato_job = None

    def _tomato_done(self):
        self._tomato_job = None
        self._tomato_left = 0
        self._tomato_chip_destroy()
        self.show_bubble("到点啦，起身活动一下喵～", 5000)

    def _tomato_chip(self, text):
        """形象左侧的白底小胶囊，不挡形象本体。"""
        try:
            if not (self._tomato_win and self._tomato_win.winfo_exists()):
                win = tk.Toplevel(self.root)
                win.overrideredirect(True)
                win.attributes("-topmost", True)
                win.configure(bg="#ffffff")
                lab = tk.Label(win, text=text, bg="#ffffff", fg="#c0392b",
                               font=("微软雅黑", 9, "bold"), padx=8, pady=3, bd=0)
                lab.pack()
                self._tomato_win, self._tomato_label = win, lab
            else:
                self._tomato_label.config(text=text)
            self._tomato_win.update_idletasks()
            w = self._tomato_win.winfo_reqwidth()
            x = self.root.winfo_x() - w - 8
            y = self.root.winfo_y() + (self._pet_size - self._tomato_win.winfo_reqheight()) // 2
            self._tomato_win.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except Exception:
            self._tomato_chip_destroy()

    def _tomato_chip_destroy(self):
        w, self._tomato_win = self._tomato_win, None
        self._tomato_label = None
        try:
            if w and w.winfo_exists():
                w.destroy()
        except Exception:
            pass

    # ---------------------------------------------------- Win+D 守护
    def _pet_hwnd(self):
        try:
            import ctypes
            h = int(self.root.winfo_id())
            top = ctypes.windll.user32.GetAncestor(h, 2)   # GA_ROOT
            return top or h
        except Exception:
            return 0

    def _pet_visible(self):
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            h = self._pet_hwnd()
            if not h:
                return True
            if not u.IsWindowVisible(h):
                return False
            if u.IsIconic(h):
                return False
            r = wintypes.RECT()
            if u.GetWindowRect(h, ctypes.byref(r)):
                if (r.right - r.left) <= 0 or (r.bottom - r.top) <= 0:
                    return False
            return True
        except Exception:
            try:
                return bool(self.root.winfo_viewable())
            except Exception:
                return True

    def _pet_guard_tick(self):
        self._pet_guard_job = None
        if self._closing:
            return
        try:
            if not self._pet_visible():
                self._pet_restore()
        except Exception:
            pass
        try:
            if not self._closing:
                self._pet_guard_job = self.root.after(self.PET_GUARD_MS, self._pet_guard_tick)
        except Exception:
            pass

    def _pet_restore(self):
        """被 Win+D 藏起来后拉回；轻量恢复无效则 withdraw+deiconify 强制重建"""
        try:
            self.root.deiconify()
            self.root.attributes("-topmost", False)
            self.root.attributes("-topmost", True)
            self.root.lift()
        except Exception:
            pass
        if not self._pet_visible():
            try:
                self.root.withdraw()
                self.root.after(30, self._pet_force_show)
            except Exception:
                pass

    def _pet_force_show(self):
        if self._closing:
            return
        try:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.lift()
        except Exception:
            pass

    # ---------------------------------------------------- 旧实例清理
    def _kill_legacy_pet(self):
        """单窗口模式：收掉历史遗留的独立 pet_window.py，避免两个形象叠在一起"""
        if os.name != "nt":
            return
        try:
            ps = ("Get-CimInstance Win32_Process | Where-Object { "
                  "$_.CommandLine -like '*pet_window.py*' } | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
            subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                            "-Command", ps],
                           capture_output=True, timeout=8,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass


    OVERLAY_BOX = 120   # 形象最大边长（像素），等比缩放

    def _load_overlay_media(self):
        """按 GIF > WEBP > PNG 优先级加载形象；动图预渲染成帧序列。"""
        try:
            from PIL import Image, ImageTk, ImageSequence
        except Exception:
            self._overlay_text_fallback()
            return

        try:
            files = sorted(os.listdir("."))
        except Exception:
            files = []

        media = None
        for ext in (".gif", ".webp", ".png"):
            for f in files:
                if f.lower().endswith(ext):
                    media = f
                    break
            if media:
                break

        if not media:
            self._overlay_text_fallback()
            return

        try:
            im = Image.open(media)
        except Exception:
            self._overlay_text_fallback()
            return

        box = self.OVERLAY_BOX

        def _fit(frame):
            frame = frame.convert("RGBA")
            w, h = frame.size
            if w <= 0 or h <= 0:
                return frame
            ratio = min(box / w, box / h, 1.0)
            nw, nh = max(1, int(w * ratio)), max(1, int(h * ratio))
            return frame.resize((nw, nh), Image.Resampling.LANCZOS)

        try:
            n_frames = int(getattr(im, "n_frames", 1))
        except Exception:
            n_frames = 1

        if n_frames <= 1:
            try:
                self.image = ImageTk.PhotoImage(_fit(im))
                self.label.config(image=self.image)
            except Exception:
                self._overlay_text_fallback()
            return

        frames, durations = [], []
        try:
            for fr in ImageSequence.Iterator(im):
                frames.append(ImageTk.PhotoImage(_fit(fr)))
                d = fr.info.get("duration", None) or im.info.get("duration", None) or 100
                durations.append(max(30, int(d)))
        except Exception:
            if not frames:
                self._overlay_text_fallback()
                return

        if len(frames) <= 1:
            self.image = frames[0]
            self.label.config(image=self.image)
            return

        self.overlay_frames = frames
        self.overlay_durations = durations
        self.image = frames[0]
        self.label.config(image=self.image)

    def _overlay_text_fallback(self):
        self.label.config(text="路夜", font=("微软雅黑", 12, "bold"),
                          bg="#1e90ff", fg="white", width=8, height=4)

    def _play_overlay_frame(self):
        """逐帧播放；帧数不足两帧时不启动。"""
        if len(self.overlay_frames) <= 1:
            return
        self.overlay_frame_idx = (self.overlay_frame_idx + 1) % len(self.overlay_frames)
        self.image = self.overlay_frames[self.overlay_frame_idx]
        try:
            self.label.config(image=self.image)
        except Exception:
            return
        self.root.after(self.overlay_durations[self.overlay_frame_idx], self._play_overlay_frame)

    def _flush_pending(self):
        """把聊天窗没开时攒下的主动性消息，一次性补显出来。"""
        if not self.pending_proactive:
            return
        try:
            self.append_to_chat("\n[系统] 你忙的时候，路夜说过这几句：\n", "sys_msg")
            for _line in self.pending_proactive:
                self.append_to_chat(_line, "ai_msg")
            self.pending_proactive.clear()
        except Exception:
            pass

    def push_proactive(self, text, meta=None):
        """主动性消息统一出口：弹气泡 + 写进对话记忆 + 落盘 + 没开窗就转未读。"""
        if not text:
            return
        meta = meta or {}
        reason = meta.get("reason") or "daily"
        is_game_end = (reason == "game_end")
        title = "路夜 · 游玩结束" if is_game_end else "路夜"
        try:
            show_windows_toast(title, text)
        except Exception:
            pass
        try:
            self.show_bubble(text)
        except Exception:
            pass
        prefix = "（游玩结束）" if is_game_end else ""
        line = "\n路夜: " + prefix + text + "\n"
        if self.chat_window and self.chat_window.winfo_exists():
            self.append_to_chat(line, "ai_msg")
        else:
            self.pending_proactive.append(line)
        # 关键：写进记忆并落盘，否则老大追问时 AI 不知道自己刚说过啥
        try:
            self.messages_context.append({"role": "assistant", "content": prefix + text})
            save_history(self.messages_context)
        except Exception:
            pass

    def pop_boot_greeting(self, text):
        """开机问候：走主动性同一套出口，保证写进记忆、不再失忆。"""
        self.push_proactive(text, {"reason": "boot"})

    def start_drag(self, event):
        self.start_x, self.start_y = event.x, event.y

    def drag(self, event):
        new_x = self.root.winfo_x() + (event.x - self.start_x)
        new_y = self.root.winfo_y() + (event.y - self.start_y)
        self.root.geometry(f"+{new_x}+{new_y}")

    def open_chat_window(self, event):
        self.chat_open_flag = True
        if self.chat_window and self.chat_window.winfo_exists():
            self.chat_window.deiconify()
            self._flush_pending()
            return

        self.chat_window = tk.Toplevel(self.root)
        self.chat_window.title("与路夜对话")

        # ---- 修复①：给窗口明确位置 + 最小尺寸，防止底部输入框被挤出屏幕 ----
        w, h = 480, 640
        sw, sh = self.chat_window.winfo_screenwidth(), self.chat_window.winfo_screenheight()
        x = max(0, sw - w - 60)
        y = max(0, sh - h - 80)
        self.chat_window.geometry(f"{w}x{h}+{x}+{y}")
        self.chat_window.minsize(360, 320)

        self.chat_window.attributes("-topmost", True)
        self.chat_window.configure(bg="#1e1e1e")

        # ---- 修复③-а：先钉底部输入区，保证它永远不被挤掉 ----
        input_frame = tk.Frame(self.chat_window, bg="#1e1e1e")
        input_frame.pack(side=tk.BOTTOM, padx=10, pady=10, fill=tk.X)

        # ---- 修复⑦：发送键先建先 pack，固定尺寸控件优先占地，永不被挤出窗口 ----
        self.send_button = tk.Button(
            input_frame,
            text="发送",
            command=self.send_message,
            bg="#3c3c3c", fg="#ffffff",
            activebackground="#4d4d4d",
            activeforeground="#ffffff",
            disabledforeground="#6a6a6a",
           # disabledbackground="#2a2a2a",
            bd=0, padx=12, pady=4,
            cursor="hand2"
        )
        self.send_button.pack(side=tk.RIGHT, padx=(10, 0))

        # ---- 修复④：输入框改多行 Text，长文本自动换行 + 自动长高（3~8 行） ----
        input_box = tk.Frame(
            input_frame, bg="#2d2d2d",
            highlightthickness=1,
            highlightbackground="#3c3c3c",
            highlightcolor="#569cd6"
        )
        input_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.input_entry = tk.Text(
            input_box,
            font=("微软雅黑", 11),
            bg="#2d2d2d", fg="#ffcc4d",
            insertbackground="#ffffff",
            bd=0, highlightthickness=0,
            width=1,
            height=self._input_rows,
            wrap=tk.WORD,
            spacing1=2, spacing3=2
        )
        self.input_entry.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4, pady=4)

        # 到 8 行封顶后才浮出滚动条
        self._input_scroll = tk.Scrollbar(
            input_box, command=self.input_entry.yview,
            bd=0, highlightthickness=0, width=8
        )
        self.input_entry.configure(yscrollcommand=self._input_scroll.set)

        self.input_entry.bind("<Return>", self.send_message)
        self.input_entry.bind("<Shift-Return>", self._input_newline)
        self.input_entry.bind("<KeyRelease>", lambda e: self._auto_grow_input())
        self.input_entry.focus_set()

        # ---- 修复②+③-b：聊天区后 pack，并显式 height=12，空间不足时缩的是它 ----
        self.chat_text_area = scrolledtext.ScrolledText(
            self.chat_window,
            wrap=tk.WORD,
            state='disabled',
            font=("微软雅黑", 10),
            bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="#ffffff",
            selectbackground="#264f78",
            selectforeground="#ffffff",
            bd=0, highlightthickness=0,
            height=12,
            spacing1=2, spacing3=4
        )
        self.chat_text_area.pack(side=tk.TOP, padx=10, pady=(10, 0), fill=tk.BOTH, expand=True)

        # ---- 字号/颜色区分：老大 > 路夜 > 系统 ----
        self.chat_text_area.tag_config(
            "user_msg",
            foreground="#ffcc4d", font=("微软雅黑", 13, "bold"),
            spacing1=6, spacing3=6, lmargin1=8, lmargin2=8
        )
        self.chat_text_area.tag_config(
            "ai_msg",
            foreground="#d4d4d4", font=("微软雅黑", 10),
            lmargin1=8, lmargin2=8
        )
        self.chat_text_area.tag_config(
            "ai_tool",
            foreground="#7d8799", font=("微软雅黑", 9, "italic"),
            lmargin1=8, lmargin2=8
        )
        self.chat_text_area.tag_config(
            "sys_msg",
            foreground="#6a9955", font=("微软雅黑", 9),
            lmargin1=8, lmargin2=8
        )
        # P1: 三点占位样式（比正文浅、斜体，不抢眼）
        self.chat_text_area.tag_config(
            "typing_dots",
            foreground="#7d8799", font=("微软雅黑", 10, "italic"),
            lmargin1=8, lmargin2=8
        )

        self.chat_window.bind("<Escape>", lambda e: self.exit_app())

        if len(self.messages_context) > 2:
            self.append_to_chat(f"📝 [系统提示：已成功加载 {len(self.messages_context)-1} 条历史记忆。]\n", "sys_msg")

        # 窗口没开时攒下的主动性消息，一开窗就补上
        self._flush_pending()

    def show_context_menu(self, event):
        menu = tk.Menu(
            self.root,
            tearoff=0,
            bg="#2d2d2d", fg="#d4d4d4",
            activebackground="#569cd6", activeforeground="#ffffff",
            font=("微软雅黑", 10), bd=0
        )
        menu.add_command(label="💬 打开对话", command=lambda: self.open_chat_window(None))
        menu.add_command(label="😴 打瞌睡 / 唤醒", command=self._pet_toggle_sleep)
        menu.add_command(label="🖼️ 重载形象", command=self._pet_reload)
        menu.add_command(label="♥ 摸摸头", command=self._pet_heart_pop)
        menu.add_command(label="番茄钟 %d 分钟 / 停止" % self.TOMATO_MINUTES,
                         command=self._tomato_toggle)
        menu.add_separator()
        menu.add_command(label="💾 保存并退出", command=self.exit_app)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def exit_app(self):
        self.chat_open_flag = False
        if self.chat_text_area and self.chat_text_area.winfo_exists():
            self.append_to_chat("\n[系统] 老大，路夜正在保存记忆并退出...\n", "sys_msg")
        save_history(self.messages_context)
        # P5-B: 停掉悬浮窗所有 after 任务，避免销毁后回调报错
        self._closing = True
        self._pet_sync_pos()
        try:
            self._pet_heart_clear()
            self._tomato_stop(None)
        except Exception:
            pass
        for _job in (self._pet_tick_job, self._pet_load_job,
                     self._pet_guard_job, self._pet_single_job,
                     self._pet_wake_job, self._heart_job, self._tomato_job):
            try:
                if _job:
                    self.root.after_cancel(_job)
            except Exception:
                pass
        # 主窗口已被隐藏，弹窗必须挂到聊天窗；没有聊天窗就不弹，免得出现看不见的模态框
        _parent = self.chat_window if (self.chat_window and self.chat_window.winfo_exists()) else None
        if _parent is not None:
            try:
                messagebox.showinfo("记忆已保存",
                                    f"路夜已安全退出。\n\n记忆文件保存在：\n{HISTORY_FILE}",
                                    parent=_parent)
            except Exception:
                pass
        self.root.destroy()
        sys.exit(0)

    def _input_newline(self, event=None):
        """Shift+Enter：手动换行，不发送。"""
        try:
            self.input_entry.insert(tk.INSERT, "\n")
            self._auto_grow_input()
        except Exception:
            pass
        return "break"

    def _auto_grow_input(self):
        """按实际显示行数在 3~8 行之间自适应长高；封顶后浮出滚动条。"""
        if not self.input_entry:
            return
        try:
            content = self.input_entry.get("1.0", "end-1c")
        except Exception:
            return

        rows = None
        try:
            cnt = self.input_entry.count("1.0", "end-1c", "displaylines")
            if cnt:
                rows = int(cnt[0])
        except Exception:
            rows = None

        if not rows:
            rows = 0
            for seg in (content.split("\n") if content else [""]):
                n = len(seg)
                rows += max(1, (n // 38) + (1 if n % 38 else 0))

        rows = min(8, max(3, rows))
        if rows != self._input_rows:
            self._input_rows = rows
            try:
                self.input_entry.configure(height=rows)
            except Exception:
                pass

        try:
            if rows >= 8 and not self._input_scroll.winfo_ismapped():
                self._input_scroll.pack(side=tk.RIGHT, fill=tk.Y)
            elif rows < 8 and self._input_scroll.winfo_ismapped():
                self._input_scroll.pack_forget()
        except Exception:
            pass

    def send_message(self, event=None):
        user_input = self.input_entry.get("1.0", tk.END).strip()
        if not user_input or self.is_typing:
            if event is not None:
                return "break"
            return

        self.input_entry.delete("1.0", tk.END)
        # 发完复位高度与滚动条
        self._input_rows = 3
        try:
            self.input_entry.configure(height=3)
            if self._input_scroll and self._input_scroll.winfo_ismapped():
                self._input_scroll.pack_forget()
        except Exception:
            pass

        self.append_to_chat(f"老大: {user_input}\n", "user_msg")
        INTERRUPT_EVENT.clear()       # P2: 新一轮，清掉上一轮残留的停止标记
        self.is_typing = True
        self._typing_deadline = time.time() + self.TYPING_IDLE_TIMEOUT
        self._set_send_button("stop")  # P2: 生成中按钮即「■ 停止」
        self._start_typing_dots()      # P1: 起三点占位

        threading.Thread(target=ai_chat_worker, args=(user_input, self.messages_context), daemon=True).start()
        if event is not None:
            return "break"

    def append_to_chat(self, text, tag=None):
        if not self.chat_text_area:
            return
        self.chat_text_area.config(state='normal')
        if tag:
            self.chat_text_area.insert(tk.END, text, tag)
        else:
            self.chat_text_area.insert(tk.END, text)
        self.chat_text_area.see(tk.END)
        self.chat_text_area.config(state='disabled')

    def check_queue(self):
        """轮询消息队列（v1.2.1 热补丁：队列轮询兜底）。单条消息失败只丢一条，轮询永不死。"""
        try:
            while True:
                try:
                    raw_msg = message_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_queue_msg(raw_msg)
                finally:
                    # 只要队列还在出货，就刷新打字超时计时（防长任务被看门狗误杀）
                    if self.is_typing:
                        self._typing_deadline = time.time() + self.TYPING_IDLE_TIMEOUT
        finally:
            # 关键：无论上面发生什么，都必定重新注册下一轮轮询（防界面永久卡死）
            self._schedule_queue_poll()

    def _handle_queue_msg(self, raw_msg):
        """处理单条队列消息；任何异常都在内部消化并记 error.log，绝不拖垮轮询。"""
        msg_type = None
        try:
            if isinstance(raw_msg, tuple):
                msg_type, content = raw_msg
            else:
                msg_type, content = "AI_CHUNK", str(raw_msg)

            if msg_type == "ALERT":
                show_windows_toast("系统警报", content)
                if self.chat_window and self.chat_window.winfo_exists():
                    self.append_to_chat(f"\n⚠️ [后台消息]: {content}\n", "sys_msg")
            elif msg_type == "AI_CHUNK":
                self._clear_typing_dots()   # P1: 首字/工具提示到达，立即撤占位
                # 工具提示行（以 [ 开头）用小号灰色，其余正文字用 AI 样式
                tag = "ai_tool" if content.strip().startswith("[") and content.strip().endswith("]") else "ai_msg"
                self.append_to_chat(content, tag)
            elif msg_type == "CMD_CONFIRM":
                self._clear_typing_dots()   # P1: 确认行出现即撤占位
                cmd, reason, why, detail, box, ev = content
                _why_show = why or "（路夜没说明目的）"
                self.append_to_chat(
                    "\n[⚠️ 危险命令待确认] " + cmd +
                    "\n   🎯 目的: " + _why_show +
                    "\n   ⚠️ 触发原因: " + reason + "\n", "sys_msg")
                try:
                    _parent = self.chat_window if (self.chat_window and self.chat_window.winfo_exists()) else self.root
                    ok = messagebox.askyesno(
                        "危险命令确认",
                        "路夜想执行:\n\n" + cmd +
                        "\n\n🎯 目的（路夜的意图）:\n" + _why_show +
                        "\n\n⚠️ 为什么会弹窗:\n" + reason +
                        ("\n\n" + detail if detail else "") +
                        "\n\n允许执行吗？", parent=_parent)
                except Exception:
                    ok = False
                box["ok"] = bool(ok)
                ev.set()
                self.append_to_chat("[系统] 老大" + ("批准" if ok else "拒绝") + "了这条命令。\n", "sys_msg")
            elif msg_type == "AI_DONE":
                self.append_to_chat("\n")
                self._finish_typing()
        except Exception:
            # 单条消息出问题：丢这一条、记日志、继续轮询
            try:
                with open("error.log", "a", encoding="utf-8") as _f:
                    _f.write("\n--- check_queue 单条消息处理失败（类型 %s）---\n" % msg_type)
                    _f.write(traceback.format_exc())
                    _f.write("\n-------------------\n")
            except Exception:
                pass
            # 只有收尾消息炸了才强解，避免误杀正在输出的轮次
            try:
                if msg_type == "AI_DONE" and self.is_typing:
                    self._finish_typing()
            except Exception:
                pass

    # ==================== P2: 强行中断 ====================
    def _set_send_button(self, state="send"):
        """发送键三态：send=可点发送 / stop=可点停止 / busy=停止中（不可点）。"""
        btn = self.send_button
        if not btn:
            return
        try:
            if state == "stop":
                btn.config(text="■ 停止", command=self.stop_generation,
                           state=tk.NORMAL, bg="#8b2b2b",
                           activebackground="#a33333")
            elif state == "busy":
                btn.config(text="停止中…", command=self.stop_generation,
                           state=tk.DISABLED, bg="#5a2a2a")
            else:
                btn.config(text="发送", command=self.send_message,
                           state=tk.NORMAL, bg="#3c3c3c",
                           activebackground="#4d4d4d")
        except Exception:
            pass

    def stop_generation(self, event=None):
        """只置标记：让 worker 在下一块 chunk / 下一轮工具前自己退出。
        绝不动 is_typing，收尾统一交给 AI_DONE -> _finish_typing。"""
        if not self.is_typing:
            return
        INTERRUPT_EVENT.set()
        self._set_send_button("busy")
        self.append_to_chat("\n[系统] 已请求停止，本轮不再继续。\n", "sys_msg")

    # ==================== P1: 三点循环（等思考提示）====================
    def _start_typing_dots(self):
        """起三点占位。聊天窗没开就静默跳过，不报错。"""
        self._clear_typing_dots()
        area = self.chat_text_area
        try:
            if not area or not area.winfo_exists():
                return
        except Exception:
            return
        self._dots_idx = 0
        self._tick_typing_dots()

    def _tick_typing_dots(self):
        """每 350ms 换一帧；帧宽固定为 "..."，整行不会左右跳。"""
        self._dots_job = None
        area = self.chat_text_area
        try:
            if not self.is_typing or not area or not area.winfo_exists():
                return
            if not (self.chat_window and self.chat_window.winfo_exists()):
                return
            area.config(state='normal')
            self._erase_dots_block(area)
            frame = self.TYPING_FRAMES[self._dots_idx % len(self.TYPING_FRAMES)]
            area.insert(tk.END, self.TYPING_PREFIX + frame, ("typing_dots",))
            area.config(state='disabled')
            if self._dots_idx == 0:
                # 只在首帧滚一次屏，之后禁滚，否则每 350ms 抖一遍
                area.see(tk.END)
            self._dots_idx += 1
        except Exception:
            pass
        try:
            self._dots_job = self.root.after(self.TYPING_DOT_INTERVAL, self._tick_typing_dots)
        except Exception:
            self._dots_job = None

    def _erase_dots_block(self, area=None):
        """删掉占位整块。占位不带前导换行，删完就回到上一行末尾，不留空行。
        注意：绝不能把起点前移 1 个字符——自测证实那样会吃掉上一行的字。"""
        area = area or self.chat_text_area
        if not area:
            return
        try:
            rng = area.tag_ranges("typing_dots")
            if not rng:
                return
            area.delete(str(rng[0]), str(rng[-1]))
        except Exception:
            pass

    def _clear_typing_dots(self):
        """撤占位。首字到达 / AI_DONE / CMD_CONFIRM / 超时 / 异常 五处统一走这里。"""
        job = self._dots_job
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._dots_job = None
        self._dots_idx = 0
        area = self.chat_text_area
        try:
            if not area or not area.winfo_exists():
                return
            if not area.tag_ranges("typing_dots"):
                return
            area.config(state='normal')
            self._erase_dots_block(area)
            area.config(state='disabled')
        except Exception:
            pass

    def _finish_typing(self):
        """统一收尾：解锁打字状态 + 恢复发送按钮（正常流程与兜底共用）。"""
        self._clear_typing_dots()   # P1: 完成 / 超时一律撤占位
        self.is_typing = False
        self._typing_deadline = 0
        self._set_send_button("send")   # P2: 收尾必恢复「发送」

    def _schedule_queue_poll(self):
        """轮询自续：出错也必定重新注册，界面不卡死。"""
        try:
            self.root.after(100, self.check_queue)
        except Exception:
            pass

    def _typing_watchdog(self):
        """兜底巡检：AI_DONE 万一真丢了，静默超时后自动解锁发送按钮。"""
        try:
            if self.is_typing and self._typing_deadline and time.time() > self._typing_deadline:
                self.append_to_chat("\n[系统] 上一轮响应静默超时，已自动解锁发送按钮（记忆仍会保存）。\n", "sys_msg")
                self._finish_typing()
        except Exception:
            pass
        try:
            self.root.after(self.TYPING_WATCH_INTERVAL * 1000, self._typing_watchdog)
        except Exception:
            pass

# ==================== 启动器与单实例锁 ====================
if __name__ == "__main__":
    try:
        # 端口改为 54322，避免和旧的 desktop_bug.py（54321）抢锁
        single_instance_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        single_instance_socket.bind(('127.0.0.1', 54322))
    except socket.error:
        sys.exit(0)

    try:
        app = DesktopBugApp()
        app.root.mainloop()
    except Exception:
        with open("error.log", "a", encoding="utf-8") as f:
            f.write("\n--- 路夜(修复版)崩溃了 ---\n")
            f.write(traceback.format_exc())
            f.write("\n-------------------\n")
