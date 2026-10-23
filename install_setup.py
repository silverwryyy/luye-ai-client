# -*- coding: utf-8 -*-
"""路夜 安装引导

由 装我.bat 调用：  %PY% "install_setup.py"

流程：查 Python 版本 -> 建 .venv -> 装 requirements.txt -> 依赖自检 -> 配置 .env（缺项当场问你）
全部通过才返回 0；任一步失败立刻返回非零，装我.bat 会停下并保留窗口。
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
VENV_PY = VENV / "Scripts" / "python.exe"
REQ = ROOT / "requirements.txt"
ENV_FILE = ROOT / ".env"

MIN_PY = (3, 10)
REQUIRED_KEYS = ["DEEPSEEK_API_KEY"]
OPTIONAL_KEYS = ["TAVILY_API_KEY"]

IMPORT_TEST = (
    "import requests, psutil, dotenv, openai, winotify, PIL, docx;"
    " print('IMPORT_OK')"
)


def say(msg=""):
    print(msg, flush=True)


def head(msg):
    say("")
    say("=" * 54)
    say("  " + msg)
    say("=" * 54)


def fail(msg):
    say("")
    say("[X] " + msg)
    say("")
    sys.exit(1)


def check_python():
    if sys.version_info < MIN_PY:
        fail(
            "当前 Python 是 %d.%d，版本太低。需要 3.10 或更高。"
            % sys.version_info[:2]
        )
    say("[OK] Python %d.%d.%d" % sys.version_info[:3])
    if sys.version_info[:2] >= (3, 14):
        say("[!] 3.14 比较新，个别包可能没有预编译轮子；")
        say("    若下面装依赖失败，建议装 Python 3.13 再跑一次本安装器。")


def make_venv():
    if VENV_PY.exists():
        say("[OK] 已有虚拟环境 .venv，直接复用，不重建。")
        return
    say("[..] 正在创建虚拟环境 .venv ...")
    r = subprocess.run([sys.executable, "-m", "venv", str(VENV)])
    if r.returncode != 0 or not VENV_PY.exists():
        fail("虚拟环境创建失败。请检查磁盘空间和目录权限。")
    say("[OK] 虚拟环境已创建。")


def pip_run(args, what):
    cmd = [str(VENV_PY), "-m", "pip"] + args
    r = subprocess.run(cmd)
    if r.returncode != 0:
        fail("%s 失败。可手动重试：\n    %s" % (what, " ".join(cmd)))
    say("[OK] " + what)


def install_deps():
    if not REQ.exists():
        fail("找不到 requirements.txt，无法安装依赖。请确认它和本文件在同一目录。")
    pip_run(["install", "--upgrade", "pip", "--quiet"], "升级 pip")
    pip_run(["install", "-r", str(REQ)], "安装 requirements.txt 里的依赖")


def selftest():
    say("[..] 正在做导入自检 ...")
    r = subprocess.run(
        [str(VENV_PY), "-c", IMPORT_TEST],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if "IMPORT_OK" not in (r.stdout or ""):
        fail(
            "依赖导入自检失败，路夜会起不来。原始输出：\n"
            + (r.stdout or "")
            + (r.stderr or "")
        )
    say("[OK] 核心包全部导入正常（requests/psutil/dotenv/openai/winotify/PIL/docx）。")


def _ask(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        say("")
        return ""


def _parse_env(text):
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        env[k.strip()] = v
    return env


def _write_env(env):
    known = list(REQUIRED_KEYS) + list(OPTIONAL_KEYS)
    lines = ["# 路夜 环境变量（install_setup.py 自动生成；只在本机用，别外传）"]
    for k in known:
        v = env.get(k, "")
        if v:
            lines.append("%s=%s" % (k, v))
        elif k in OPTIONAL_KEYS:
            lines.append("# %s=" % k)
    for k, v in env.items():
        if k not in known and v:
            lines.append("%s=%s" % (k, v))
    data = ("\n".join(lines) + "\n").encode("utf-8")
    tmp = ROOT / ".env.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, ENV_FILE)


def ensure_env():
    head("配置 API Key")
    env = {}
    if ENV_FILE.exists():
        env = _parse_env(ENV_FILE.read_text(encoding="utf-8", errors="ignore"))
        say("[OK] 已找到 .env：" + str(ENV_FILE))
    else:
        say("[..] 还没有 .env，现在帮你建一个。")
        say("     一共两项：一项必填、一项可选，回车就能过。")

    changed = False
    for k in REQUIRED_KEYS:
        if env.get(k):
            say("[OK] %s 已配置（%d 位）" % (k, len(env[k])))
            continue
        say("")
        say("[必填] %s" % k)
        say("       申请地址：https://platform.deepseek.com/api_keys")
        val = _ask("       粘贴密钥后回车（直接回车 = 放弃安装）: ")
        if not val:
            say("")
            fail(
                "没有 %s，路夜一启动就会报错。补法二选一："
                "  1) 再双击一次 装我.bat   2) 手动在 %s 里写一行 %s=你的密钥"
                % (k, ENV_FILE.name, k)
            )
        env[k] = val
        changed = True
        say("[OK] %s 已记录（%d 位）" % (k, len(val)))

    for k in OPTIONAL_KEYS:
        if env.get(k):
            say("[OK] %s 已配置（%d 位）" % (k, len(env[k])))
            continue
        say("")
        say("[可选] %s —— 联网搜索要用，申请：https://tavily.com" % k)
        val = _ask("       粘贴后回车，直接回车就跳过（不影响聊天）: ")
        if val:
            env[k] = val
            changed = True
            say("[OK] %s 已记录（%d 位）" % (k, len(val)))
        else:
            say("[--] 跳过 %s，web_search 工具会不可用" % k)

    if changed or not ENV_FILE.exists():
        _write_env(env)
        say("[OK] 已写入 " + str(ENV_FILE) + "（UTF-8，无 BOM）")


def summary():
    head("安装完成")
    say("下一步：")
    say("  1) 双击 启动路夜.bat  -> 桌面右下角出现小圆")
    say("  2) 单击小圆 = 摸头；双击小圆 = 打开对话窗；右键 = 菜单")
    say("  3) 想清静时右键菜单里选「打瞌睡」，光标压在小圆上按一下右键唤醒")
    say("")
    say("忘了配 key？再双击一次 装我.bat，或手动编辑 .env。")
    say("")


def main():
    head("路夜 安装引导")
    say("安装目录：" + str(ROOT))
    check_python()
    make_venv()
    install_deps()
    selftest()
    ensure_env()
    summary()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("")
        say("[X] 已取消。")
        sys.exit(1)
