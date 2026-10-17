# -*- coding: utf-8 -*-
"""
路夜 (Luye v1.0) 安装助手
由「装我.bat」调用：建 .venv -> 装依赖 -> 检查 .env -> 打印下一步。

为什么中文放在这里而不是 .bat 里：
cmd 解析 UTF-8 编码的 .bat 会按字节切行，中文提示会被劈成乱码命令，
所以 .bat 只留纯 ASCII 骨架，所有给朋友看的中文都由本脚本打印。
"""
import subprocess
import sys
from pathlib import Path

try:
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
VPY = VENV / "Scripts" / "python.exe"
REQ = HERE / "requirements.txt"
ENV = HERE / ".env"
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

PLACEHOLDER = "填自己申请的对应api"

ENV_TEMPLATE = """# 填入你自己申请的 API Key
# 申请地址：DeepSeek -> https://platform.deepseek.com/   Tavily -> https://tavily.com/
DEEPSEEK_API_KEY={p}
TAVILY_API_KEY={p}
""".format(p=PLACEHOLDER)


def run(cmd):
    """跑一条外部命令，实时把输出吐到控制台。"""
    print("      $ " + " ".join(str(c) for c in cmd))
    return subprocess.call([str(c) for c in cmd])


def venv_foreign():
    """
    .venv 是不是别的电脑（或别的 Python）建的？
    判据：pyvenv.cfg 里的 home 指向的解释器目录在本机不存在。
    是外来户就必须重建，否则会拿一个路径全错的假环境去跑。
    """
    cfg = VENV / "pyvenv.cfg"
    if not cfg.exists():
        return True
    try:
        txt = cfg.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return True
    for line in txt.splitlines():
        if line.lower().startswith("home"):
            home = line.split("=", 1)[-1].strip()
            if home:
                return not Path(home).exists()
    return False


def main():
    print("=" * 58)
    print("            路 夜  (Luye v1.0)  安装助手")
    print("=" * 58)
    print("  只在本文件夹内活动，不动系统别处。")
    print()

    # ---------- 2. 建虚拟环境 ----------
    if VPY.exists() and not venv_foreign():
        print("[2/4] 已有 .venv，跳过创建")
    else:
        if VPY.exists():
            print("[2/4] 检测到 .venv 是别的电脑上建的，自动重建 ...")
        else:
            print("[2/4] 正在创建虚拟环境 .venv ...")
        if run([sys.executable, "-m", "venv", "--clear", str(VENV)]) != 0:
            print()
            print("[X] 创建 .venv 失败，把上面的报错截图发给老大。")
            return 1
    print()

    # ---------- 3. 装依赖 ----------
    if not REQ.exists():
        print("[X] 没找到 requirements.txt，包不完整，重新拷一份。")
        return 1
    print("[3/4] 安装依赖（走清华镜像加速，首次约 1~3 分钟，请耐心等）")
    run([VPY, "-m", "pip", "install", "--upgrade", "pip", "-i", MIRROR, "--quiet"])
    if run([VPY, "-m", "pip", "install", "-r", str(REQ), "-i", MIRROR]) != 0:
        print()
        print("[X] 依赖安装失败。八成是网络抖动：重新双击「装我.bat」再跑一次即可；")
        print("    还不行就把报错截图发给老大。")
        return 1
    print()

    # ---------- 4. 检查 .env ----------
    print("[4/4] 检查配置文件 .env")
    if not ENV.exists():
        print("      [!] 没找到 .env，已生成空白模板。")
        try:
            ENV.write_text(ENV_TEMPLATE, encoding="utf-8")
        except Exception as e:
            print("      [X] 写 .env 失败：%s" % e)

    need_key = True
    if ENV.exists():
        try:
            txt = ENV.read_text(encoding="utf-8", errors="ignore")
            if PLACEHOLDER not in txt:
                need_key = False
        except Exception:
            pass

    print()
    print("=" * 58)
    print("  安装完成。")
    print()
    if need_key:
        print("  [!] 重要：.env 里的 Key 还没填，现在还不能聊天。")
        print("      用记事本打开 .env，把「%s」" % PLACEHOLDER)
        print("      换成你自己的真 Key 再存盘：")
        print("        DeepSeek 申请 -> https://platform.deepseek.com/")
        print("        Tavily   申请 -> https://tavily.com/")
    else:
        print("  [OK] .env 看起来已经填好了。")
    print()
    print("  下一步：双击「启动路夜.bat」开窗。")
    print()
    print("  可选：")
    print("    - 想开机自动打招呼：把 boot_greet.py 的快捷方式丢进")
    print("      Win+R 输入 shell:startup 打开的那个文件夹。")
    print("    - 想每晚自动归拢日程：把 nightly_organize.py 加进计划任务，")
    print("      每晚 23:30 跑一次。")
    print("=" * 58)
    return 0


if __name__ == "__main__":
    sys.exit(main())
