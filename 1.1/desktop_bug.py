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
import time
import queue
import socket
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

# ==================== 记忆持久化与折叠机制 ====================
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json")
SAVE_ERROR_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "save_error.log")

MAX_HISTORY_MESSAGES = 20
KEEP_RECENT_MESSAGES = 8

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    return data
        except Exception:
            pass
    # 强化系统提示词，强迫AI必须分页读完文件，并且支持多轮调用
    return [{"role": "system", "content": "你是老大的赛博助手，名叫路夜。拥有联网搜索、扫描本地目录、读取本地文件、写入本地文件的能力。回答要简短精干。当你需要执行操作时，必须调用工具，不要只用文字描述你要做什么。**调用工具时，请严格按照工具定义发送工具调用请求，不要将工具调用写成文本。** 当你读取代码文件时，如果文件较长，请分块读取。**如果你调用 read_file 发现提示有后续内容，你必须立即再次调用 read_file 继续读，直到读完为止，才能给出结论。** 当你需要写文件时，必须先告诉老大你要写的路径和内容，得到确认后再调用工具。严禁写入或覆盖系统盘的核心文件！"}]

def save_history(messages):
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
    if len(messages_context) <= MAX_HISTORY_MESSAGES:
        return

    message_queue.put(("AI_CHUNK", "\n[🧠 记忆超载，路夜正在自动总结并折叠历史对话...]\n"))

    system_prompt = messages_context[0]
    to_summarize = messages_context[1:-KEEP_RECENT_MESSAGES]
    recent_messages = messages_context[-KEEP_RECENT_MESSAGES:]

    summary_prompt = "请将以下历史对话压缩成一段简短的摘要，保留用户的核心诉求、AI已完成的动作和关键结论，以便AI在后续对话中保持记忆连续性。历史对话：\n"
    for msg in to_summarize:
        role_name = "用户" if msg.get("role") == "user" else "AI"
        text = msg.get("content") or ""
        if not text:
            continue
        summary_prompt += f"{role_name}: {text}\n"

    try:
        response = client.chat.completions.create(
            model="deepseek-flash",
            messages=[{"role": "user", "content": summary_prompt}],
            stream=False
        )
        summary_text = response.choices[0].message.content.strip()

        folded_context = [
            system_prompt,
            {"role": "system", "content": f"【历史对话摘要】：{summary_text}"}
        ] + recent_messages

        message_queue.put(("AI_CHUNK", "\n[✅ 记忆折叠完成，路夜已续接上下文。]\n"))

        messages_context.clear()
        messages_context.extend(folded_context)
    except Exception:
        messages_context.clear()
        messages_context.extend([system_prompt] + recent_messages)

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
    resp = client.chat.completions.create(model="deepseek-flash", messages=temp_messages, stream=True)
    for chunk in resp:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if not delta:
            continue
        full_answer += delta
        pending += delta

        m = _STRUCT_PAT.search(pending)
        if m:
            frag = m.group(0)
            log_hit("stream", frag, full_answer)
            suppress_left = 1500          # 命中真结构就开长闸门，不看是不是 DSML
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
            message_queue.put(("AI_CHUNK", safe))

    if pending and suppress_left <= 0:
        safe_all += pending
        message_queue.put(("AI_CHUNK", pending))

    clean = _sanitize_tool_text(full_answer)
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
    MAX_TOOL_ROUNDS = 20
    called_signatures = {}   # 防死循环：同样的调用重复超过 2 次就不再强推
    full_answer = ""

    try:
        for round_num in range(MAX_TOOL_ROUNDS):
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
                        result = dispatch_tool(name, args)

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
                        result = dispatch_tool(tool_to_call, tool_args)
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
                    _result = dispatch_tool(_name, _args)
                temp_messages.append({"role": "user", "content": "【工具执行结果】: " + _result + chr(10) * 2 + "请根据以上结果继续。如果任务未完成，请继续调用工具。"})
                continue
            if _retry:
                log_hit("stream_retry", "retry", "")
                temp_messages.append({"role": "user", "content": "[系统] 上一条回答被判定为无效工具标记残渣，已丢弃。请直接用自然语言重新回答，不要输出任何标记。"})
                continue
            full_answer = _text or ""
            break

        if full_answer:
            messages_context.append({"role": "assistant", "content": full_answer})
        else:
            # 补丁⑤: 整轮啥都没产出 -> 明确提示, 而不是静默空白
            log_hit("final_empty", "", "")
            msg = "（路夜这轮没憋出内容，模型可能返回了空响应。老大再问一句试试？）"
            message_queue.put(("AI_CHUNK", msg + "\n"))
            messages_context.append({"role": "assistant", "content": msg})

    except Exception as e:
        message_queue.put(("AI_CHUNK", f"\n[❌ 路夜大脑短路了: {str(e)}]\n"))
    finally:
        save_history(messages_context)
        message_queue.put(("AI_DONE", None))

# ==================== 主界面（GUI）模块 ====================
class DesktopBugApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("赛博蛀虫·修复版")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.9)

        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.window_width, self.window_height = 100, 100
        self.root.geometry(f"{self.window_width}x{self.window_height}+{sw - 150}+{sh - 200}")

        # ---- 悬浮窗形象：优先 GIF/WEBP 动图，其次静态图，最后纯文字兜底 ----
        self.overlay_frames = []          # 动图各帧（已预缩放）
        self.overlay_durations = []       # 各帧时长（毫秒）
        self.overlay_frame_idx = 0
        self.image = None

        self.label = tk.Label(self.root, bg="white", bd=0)
        self._load_overlay_media()

        self.label.pack(fill=tk.BOTH, expand=True)

        # 窗口尺寸贴合实际帧，避免非方形 GIF 留白
        if self.image is not None:
            try:
                self.window_width, self.window_height = self.image.width(), self.image.height()
                self.root.geometry(f"{self.window_width}x{self.window_height}+{sw - 150}+{sh - 200}")
            except Exception:
                pass

        # 多帧则启动动画循环
        if len(self.overlay_frames) > 1:
            self.root.after(self.overlay_durations[0], self._play_overlay_frame)

        self.label.bind("<Button-1>", self.start_drag)
        self.label.bind("<B1-Motion>", self.drag)
        self.label.bind("<Double-Button-1>", self.open_chat_window)
        self.label.bind("<Button-3>", self.show_context_menu)

        self.start_x, self.start_y = 0, 0
        self.messages_context = load_history()

        self.chat_window = None
        self.chat_text_area = None
        self.input_entry = None
        self.send_button = None
        self.is_typing = False

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

    # ==================== 悬浮窗形象：GIF/WEBP 动图 / 静态图 / 文字兜底 ====================
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

        self.input_entry = tk.Entry(
            input_frame,
            font=("微软雅黑", 11),
            bg="#2d2d2d", fg="#ffcc4d",
            insertbackground="#ffffff",
            bd=0, highlightthickness=1,
            highlightbackground="#3c3c3c",
            highlightcolor="#569cd6"
        )
        self.input_entry.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, ipady=4)
        self.input_entry.bind("<Return>", self.send_message)

        self.send_button = tk.Button(
            input_frame,
            text="发送",
            command=self.send_message,
            bg="#3c3c3c", fg="#ffffff",
            activebackground="#4d4d4d",
            activeforeground="#ffffff",
            bd=0, padx=12, pady=4,
            cursor="hand2"
        )
        self.send_button.pack(side=tk.RIGHT, padx=(10, 0))

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
        messagebox.showinfo("记忆已保存", f"路夜已安全退出。\n\n记忆文件保存在：\n{HISTORY_FILE}")
        self.root.destroy()
        sys.exit(0)

    def send_message(self, event=None):
        user_input = self.input_entry.get().strip()
        if not user_input or self.is_typing:
            return

        self.input_entry.delete(0, tk.END)
        self.append_to_chat(f"老大: {user_input}\n", "user_msg")
        self.is_typing = True
        self.send_button.config(state=tk.DISABLED)

        threading.Thread(target=ai_chat_worker, args=(user_input, self.messages_context), daemon=True).start()

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
        try:
            while True:
                raw_msg = message_queue.get_nowait()
                if isinstance(raw_msg, tuple):
                    msg_type, content = raw_msg
                else:
                    msg_type, content = "AI_CHUNK", str(raw_msg)

                if msg_type == "ALERT":
                    show_windows_toast("系统警报", content)
                    if self.chat_window and self.chat_window.winfo_exists():
                        self.append_to_chat(f"\n⚠️ [后台消息]: {content}\n", "sys_msg")
                elif msg_type == "AI_CHUNK":
                    # 工具提示行（以 [ 开头）用小号灰色，其余正文字用 AI 样式
                    tag = "ai_tool" if content.strip().startswith("[") and content.strip().endswith("]") else "ai_msg"
                    self.append_to_chat(content, tag)
                elif msg_type == "CMD_CONFIRM":
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
                    self.is_typing = False
                    if self.send_button:
                        self.send_button.config(state=tk.NORMAL)
        except queue.Empty:
            pass

        self.root.after(100, self.check_queue)

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
