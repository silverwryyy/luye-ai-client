# -*- coding: utf-8 -*-
"""
路夜 (Luye v1.1) 可选组件安装器
由「安装可选组件.bat」调用，也可以自己跑：
    python install_optional.py              进入菜单
    python install_optional.py officecli    只装 OfficeCLI
    python install_optional.py ppt          只装 ppt-master
    python install_optional.py all          两个都装
    python install_optional.py status       看现在装了什么

为什么中文放在这里而不是 .bat 里：
cmd 解析 UTF-8 编码的 .bat 会按字节切行，中文提示会被劈成乱码命令，
所以 .bat 只留纯 ASCII 骨架，所有给朋友看的中文都由本脚本打印。

这里只有两样「可选」的东西——路夜主程序不装它们照样能跑：
    1) OfficeCLI  : 官方单二进制，装了才能细读细写 docx / xlsx / pptx
    2) ppt-master : 做 PPT 的组件仓库，tools\\bin 那 14 个包装器靠它

两个都从上游官方仓库现拉，不随包分发。
"""
from __future__ import annotations

import subprocess
import sys
import urllib.request
from pathlib import Path

try:
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent

# ---------- OfficeCLI ----------
OC_DIR = HERE / "tools" / "officecli"
OC_EXE = OC_DIR / "officecli.exe"
OC_MIN = 10 * 1024 * 1024  # 比这还小 = 下到了错误页，不是二进制
OC_URLS = [
    ("GitHub 官方", "https://github.com/iOfficeAI/OfficeCLI/releases/latest/download/officecli-win-x64.exe"),
    ("gh-proxy 加速", "https://gh-proxy.com/https://github.com/iOfficeAI/OfficeCLI/releases/latest/download/officecli-win-x64.exe"),
]

# ---------- ppt-master ----------
PPT_PY = HERE / "fetch_ppt_master.py"
PPT_DIR = HERE / "ppt-master"
PPT_MARK = PPT_DIR / "skills" / "ppt-master" / "SKILL.md"

UA = {"User-Agent": "Mozilla/5.0 (luye-optional-installer)"}
LINE = "=" * 62


def head(title: str) -> None:
    print()
    print(LINE)
    print("  " + title)
    print(LINE)


def ok(msg: str) -> None:
    print("    [OK] " + msg)


def note(msg: str) -> None:
    print("    [i]  " + msg)


def warn(msg: str) -> None:
    print("    [!]  " + msg)


def human(n) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d B" % n) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


def oc_ready() -> bool:
    try:
        return OC_EXE.exists() and OC_EXE.stat().st_size >= OC_MIN
    except OSError:
        return False


def ppt_ready() -> bool:
    return PPT_MARK.exists()


def fetch(url: str, dest: Path) -> None:
    """流式下载到 .part，成功再改名——中途断了不会留半截 exe。"""
    tmp = dest.with_name(dest.name + ".part")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        shown = 0
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                if got - shown >= max(total // 20, 1) or got == total:
                    shown = got
                    pct = got * 100 // total
                    bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
                    print("\r    [%s] %3d%%  %s / %s" % (bar, pct, human(got), human(total)), end="", flush=True)
            elif got - shown >= 20 * 1024 * 1024:
                shown = got
                print("\r    已下载 %s" % human(got), end="", flush=True)
    print()
    tmp.replace(dest)


def install_officecli() -> int:
    head("可选组件 1/2 · OfficeCLI（约 32MB）")
    note("官方单二进制，Apache 2.0。装了路夜才能细读细写 docx / xlsx / pptx。")
    if oc_ready():
        ok("已经有了：%s（%s）" % (OC_EXE, human(OC_EXE.stat().st_size)))
        note("想重下就先把它删掉，再跑一次本脚本。")
        return 0

    for name, url in OC_URLS:
        print("  试 %s …" % name)
        try:
            fetch(url, OC_EXE)
        except Exception as e:
            warn("%s 失败（%s: %s），换下一个源" % (name, type(e).__name__, e))
            continue
        if oc_ready():
            ok("下载完成：%s（%s）" % (OC_EXE, human(OC_EXE.stat().st_size)))
            ok("来源：%s" % name)
            print()
            note("一条硬规矩：别去跑 officecli install / skills / mcp register，")
            note("          那几条会往你机器上的 AI 助手塞技能文件、改它的接线。")
            note("          想核对可以自己去官方仓库比对 SHA256SUMS。")
            return 0
        warn("下到的文件太小，八成是错误页，换下一个源")
        try:
            OC_EXE.unlink()
        except OSError:
            pass

    warn("两个源都没成，网络这边卡住了。手动兜底：")
    print("    1. 浏览器打开  %s" % OC_URLS[0][1])
    print("    2. 下到的 officecli-win-x64.exe 改名成 officecli.exe")
    print("    3. 放进  %s" % OC_DIR)
    return 1


def install_ppt() -> int:
    head("可选组件 2/2 · ppt-master（约 55MB）")
    note("做 PPT 的组件仓库。装完 tools\\bin 里那 14 个 .cmd 包装器才不是空枪。")
    if not PPT_PY.exists():
        warn("没找到 fetch_ppt_master.py，这个包不完整，重新拷一份再跑。")
        return 1
    if ppt_ready():
        ok("已经有了：%s" % PPT_DIR)
        note("想覆盖成干净版，就自己跑：python fetch_ppt_master.py")
    print()
    rc = subprocess.call([sys.executable, str(PPT_PY)])
    if rc == 0:
        ok("ppt-master 收工。")
        note("它依赖链里有 PyMuPDF（AGPL 协议），只建议本机自用，别拿去做再分发。")
    else:
        warn("没跑成（退出码 %s）。重跑一次试试，或把上面的报错截图发老大。" % rc)
    return rc


def status() -> None:
    head("当前状态")
    print("    OfficeCLI ：%s" % ("已装，%s" % human(OC_EXE.stat().st_size) if oc_ready() else "未装"))
    print("    ppt-master：%s" % ("已装" if ppt_ready() else "未装"))
    print()


MENU = """
  可选组件——不装，路夜也能聊天、也能跑：

    [1] 安装 OfficeCLI     约 32MB   让路夜能细读写 Word / Excel / PPT
    [2] 安装 ppt-master    约 55MB   让路夜能帮你做 PPT
    [3] 两个都装
    [s] 看看现在装了什么
    [0] 退出
"""


def menu() -> int:
    while True:
        head("路夜 · 可选组件安装器")
        print(MENU)
        try:
            c = input("    选择 [0/1/2/3/s]（直接回车 = 退出）：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if c in ("", "0", "q"):
            print("    好，什么都没动。路夜照样能跑。")
            return 0
        if c == "1":
            install_officecli()
        elif c == "2":
            install_ppt()
        elif c == "3":
            rc1 = install_officecli()
            rc2 = install_ppt()
            if rc1 or rc2:
                warn("上面有没成的项，对着提示再来一次。")
        elif c == "s":
            status()
        else:
            warn("没这个选项，重来。")
        print()


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    if not args:
        return menu()
    a = args[0]
    if a in ("all", "3"):
        rc1 = install_officecli()
        rc2 = install_ppt()
        return rc1 or rc2
    if a in ("officecli", "oc", "1"):
        return install_officecli()
    if a in ("ppt", "ppt-master", "2"):
        return install_ppt()
    if a in ("status", "s"):
        status()
        return 0
    print("用法：python install_optional.py [officecli|ppt|all|status]（不带参数 = 菜单）")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中途按停了，没留下半成品（下载中的 .part 下次会被覆盖）。")
        sys.exit(130)
