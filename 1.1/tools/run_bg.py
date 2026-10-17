#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_bg.py —— 后台长任务运行器（专治 run_command 超时卡死）

【为什么有它】
run_command 单次最多 300 秒就掐断。跑大下载 / 大编译 / 长脚本时一旦超时，
前台进程被掐断却可能挂着句柄不释放，导致整轮对话锁死、只能重启进程。
本脚本把长任务丢到「分离的后台进程」里执行：
  - start 立即返回，命令本身**秒回**，永远不会撞超时；
  - 之后用 status / tail 秒级轮询进度和退出码，全程不阻塞。

【用法】
  python run_bg.py start  <名字> -- <命令与参数...>   # 后台启动
  python run_bg.py status <名字> [行数]               # 状态 + 日志尾部(默认40行)
  python run_bg.py tail   <名字> [行数]               # 只看日志尾部
  python run_bg.py stop   <名字>                      # 杀掉该任务(含子进程树)
  python run_bg.py list                               # 列全部任务

【产物】（都在本包根目录的 logs 目录下）
  <名字>.log   实时输出(统一 UTF-8)
  <名字>.pid   后台执行者 PID
  <名字>.done  完成标记，内容是退出码（出现即代表已结束）

【示例】
  python run_bg.py start dl -- curl -L -o big.zip https://example.com/big.zip
  python run_bg.py status dl
"""
import os
import sys
import time
import locale
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
LOG_DIR = os.path.join(ROOT, "logs")

# Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP —— 与父进程彻底脱钩
DETACHED = 0x00000008 | 0x00000200
STILL_ACTIVE = 259


def paths(name):
    return (
        os.path.join(LOG_DIR, name + ".log"),
        os.path.join(LOG_DIR, name + ".pid"),
        os.path.join(LOG_DIR, name + ".done"),
    )


def _out(text):
    """安全打印：控制台编码(GBK)扛不住时用 replace 兜底，绝不因编码崩溃。"""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        buf = sys.stdout.buffer
        buf.write(text.encode(enc, "replace"))
        buf.flush()
    except Exception:  # noqa: BLE001
        try:
            sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
        except Exception:  # noqa: BLE001
            pass


def _alive(pid):
    """非破坏性判断进程是否存活（绝不用 os.kill(pid,0)，Windows 上那会直接杀进程）。"""
    if not pid:
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    except Exception:  # noqa: BLE001
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False


def _read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def _run(name, cmd):
    """后台执行者：跑命令 -> 逐行解码 -> 统一 UTF-8 落日志 -> 落退出码。"""
    log, pidf, donef = paths(name)
    codec = locale.getpreferredencoding(False) or "utf-8"
    rc = 900
    with open(log, "w", encoding="utf-8", errors="replace") as f:
        f.write("# CMD : %s\n" % " ".join(cmd))
        f.write("# START: %s\n\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        f.flush()
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            assert p.stdout is not None
            with p.stdout:
                for raw in iter(p.stdout.readline, b""):
                    f.write(raw.decode(codec, "replace"))
                    f.flush()
            rc = p.wait()
        except Exception as e:  # noqa: BLE001
            f.write("\n[run_bg] launch failed: %r\n" % (e,))
            rc = 900
    with open(donef, "w", encoding="utf-8") as f:
        f.write(str(rc))
    try:
        os.remove(pidf)
    except OSError:
        pass


def start(name, cmd):
    os.makedirs(LOG_DIR, exist_ok=True)
    log, pidf, donef = paths(name)
    for p in (log, pidf, donef):
        try:
            os.remove(p)
        except OSError:
            pass
    args = [sys.executable, os.path.abspath(__file__), "_run", name, "--"] + list(cmd)
    flags = DETACHED if os.name == "nt" else 0
    p = subprocess.Popen(
        args,
        creationflags=flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    with open(pidf, "w", encoding="utf-8") as f:
        f.write(str(p.pid))
    _out("started  name=%s  pid=%s\n" % (name, p.pid))
    _out("log      %s\n" % log)
    _out("poll     python run_bg.py status %s\n" % name)


def tail(name, n=40):
    log, _, _ = paths(name)
    if not os.path.exists(log):
        _out("(no log for %s)\n" % name)
        return
    with open(log, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    _out("".join(lines[-int(n):]))
    if not lines[-1:]:
        return
    if not "".join(lines[-int(n):]).endswith("\n"):
        _out("\n")


def status(name, n=40):
    log, pidf, donef = paths(name)
    if os.path.exists(donef):
        _out("STATE=done  exit=%s\n" % _read(donef, "?"))
    elif os.path.exists(pidf):
        pid = _read(pidf)
        if _alive(pid):
            _out("STATE=running  pid=%s\n" % pid)
        else:
            _out("STATE=stale(进程没了但无完成标记)  pid=%s\n" % pid)
    else:
        _out("STATE=unknown(无 pid / done 标记)\n")
    _out("-" * 48 + "\n")
    tail(name, n)


def stop(name):
    log, pidf, donef = paths(name)
    pid = _read(pidf)
    if pid and _alive(pid):
        subprocess.call(["taskkill", "/PID", pid, "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _out("killed  %s  pid=%s\n" % (name, pid))
    else:
        _out("not running: %s\n" % name)
    for p in (pidf, donef):
        try:
            os.remove(p)
        except OSError:
            pass


def list_tasks():
    if not os.path.isdir(LOG_DIR):
        _out("(no logs dir)\n")
        return
    names = sorted({f.rsplit(".", 1)[0] for f in os.listdir(LOG_DIR)
                    if f.endswith((".log", ".pid", ".done"))})
    if not names:
        _out("(no tasks)\n")
        return
    for nm in names:
        _, pidf, donef = paths(nm)
        if os.path.exists(donef):
            state = "done(exit=%s)" % _read(donef, "?")
        elif os.path.exists(pidf) and _alive(_read(pidf)):
            state = "running(pid=%s)" % _read(pidf)
        else:
            state = "stale"
        _out("%-20s %s\n" % (nm, state))


def main():
    if len(sys.argv) < 2:
        _out(__doc__ + "\n")
        return 1
    action = sys.argv[1]

    if action == "_run":
        name = sys.argv[2]
        rest = sys.argv[4:] if len(sys.argv) > 3 and sys.argv[3] == "--" else sys.argv[3:]
        _run(name, rest)
        return 0

    if action == "start":
        if len(sys.argv) < 4:
            _out("usage: run_bg.py start <name> -- <cmd...>\n")
            return 1
        name = sys.argv[2]
        rest = sys.argv[4:] if sys.argv[3] == "--" else sys.argv[3:]
        if not rest:
            _out("usage: run_bg.py start <name> -- <cmd...>\n")
            return 1
        start(name, rest)
        return 0

    if action == "status":
        status(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else 40)
        return 0

    if action == "tail":
        tail(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else 40)
        return 0

    if action == "stop":
        stop(sys.argv[2])
        return 0

    if action == "list":
        list_tasks()
        return 0

    _out("unknown action: %s\n" % action)
    _out(__doc__ + "\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
