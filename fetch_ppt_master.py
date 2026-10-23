#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
路夜 · ppt-master 组件下载器
=================================
给朋友机用：把 ppt-master 拉到本包根目录下的 ppt-master，并装齐依赖。
装完之后，tools\\bin 下那 14 个 .cmd 包装器就不再是空枪。

来源（官方仓库）：https://github.com/hugohe3/ppt-master

用法（双击 下载PPT组件.bat 等于不带参数运行）：
    python fetch_ppt_master.py             下载 + 装依赖（已存在则覆盖成干净版）
    python fetch_ppt_master.py --check     只测各路源通不通、多快，不下载
    python fetch_ppt_master.py --skip      已存在就跳过，不重下
    python fetch_ppt_master.py --no-deps   只下文件，不装依赖
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "ppt-master"
MARKER = ("skills", "ppt-master", "SKILL.md")
ZIP_NAME = "_ppt_master_download.zip"
UNZIP_DIR = "_ppt_unzip"
KEEP_DIR = "_ppt_keep"

ASSET = "https://codeload.github.com/hugohe3/ppt-master/zip/refs/heads/main"
SOURCES = [
    ("GitHub 官方", ASSET),
    ("加速 gh-proxy.com", "https://gh-proxy.com/" + ASSET),
    ("AtomGit 镜像", "https://atomgit.com/hugohe3/ppt-master/archive/main.zip"),
]
UA = {"User-Agent": "Mozilla/5.0 (luy-ppt-downloader)"}

LINE = "=" * 62


def head(title: str) -> None:
    print()
    print(LINE)
    print(f"  {title}")
    print(LINE)


def step(msg: str) -> None:
    print(f"\n>>> {msg}")


def ok(msg: str) -> None:
    print(f"    [OK] {msg}")


def note(msg: str) -> None:
    print(f"    [i]  {msg}")


def warn(msg: str) -> None:
    print(f"    [!]  {msg}")


def human(n: int) -> str:
    if n <= 0:
        return "未知大小"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def installed() -> bool:
    return TARGET.joinpath(*MARKER).exists()


def probe(url: str, timeout: int = 20):
    """返回 (状态码, 总大小, 首字节耗时)。HEAD 被拒就退化成偷 1KB 的 Range 请求。"""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=UA, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, int(r.headers.get("Content-Length") or 0), time.time() - t0
    except urllib.error.HTTPError as e:
        if e.code not in (400, 403, 405, 501):
            return e.code, 0, time.time() - t0
    except Exception:
        return 0, 0, time.time() - t0

    try:
        req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-1023"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(512)
            cr = r.headers.get("Content-Range") or ""
            m = re.search(r"/(\d+)\s*$", cr)
            return r.status, int(m.group(1)) if m else 0, time.time() - t0
    except Exception:
        return 0, 0, time.time() - t0


def do_check() -> int:
    head("源可用性检测（只发探测包，不下载）")
    live = []
    for name, url in SOURCES:
        code, size, dt = probe(url)
        if code in (200, 206):
            ok(f"{name:<18} HTTP {code}  {human(size):>10}  首包 {dt:.2f}s  {url}")
            live.append(name)
        else:
            warn(f"{name:<18} HTTP {code or '连不上':<4}  跳过  {url}")
    print()
    if live:
        note(f"可用源 {len(live)} 个，正式下载会按顺序挑第一个通的：{' / '.join(live)}")
        return 0
    warn("四个源全不通。八成是没网或被防火墙拦了，换个网络再试。")
    print(f"    手动兜底：浏览器打开 {SOURCES[0][1]} 下载 ZIP，")
    print(f"    解压后把里面的文件夹改名成 ppt-master，放到 {ROOT}")
    return 1


def download(url: str) -> Path:
    dest = ROOT / ZIP_NAME
    if dest.exists():
        dest.unlink()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        last_shown = 0
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                if got - last_shown >= max(total // 20, 1) or got == total:
                    last_shown = got
                    pct = got * 100 // total
                    bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
                    print(f"\r    [{bar}] {pct:3d}%  {human(got)} / {human(total)}", end="", flush=True)
            elif got - last_shown >= 20 * 1024 * 1024:
                last_shown = got
                print(f"\r    已下载 {human(got)}", end="", flush=True)
    print()
    ok(f"下载完成，{human(got)}")
    return dest


def fetch() -> Path:
    step("挑源下载 ppt-master（约 55MB，慢的话喝口水）")
    for name, url in SOURCES:
        print(f"  试 {name} …")
        try:
            zip_path = download(url)
            if zip_path.stat().st_size < 1024 * 1024:
                raise RuntimeError("文件太小，八成是错误页不是压缩包")
            ok(f"来源：{name}")
            return zip_path
        except Exception as e:
            warn(f"{name} 失败（{type(e).__name__}: {e}），换下一个源")
    raise SystemExit("所有源都失败。请照 --check 结尾提示，手动下 ZIP 放进本目录。")


def unpack(zip_path: Path) -> None:
    step("解压并摆放位置")
    keep = None
    if TARGET.exists():
        keep = ROOT / KEEP_DIR
        if keep.exists():
            shutil.rmtree(keep, ignore_errors=True)
        keep.mkdir(parents=True)
        moved = []
        for name in (".env", "projects"):
            src = TARGET / name
            if src.exists():
                dst = keep / name
                shutil.copytree(src, dst) if src.is_dir() else shutil.copy2(src, dst)
                moved.append(name)
        if moved:
            note("旧目录里留一手：" + "、".join(moved))
        shutil.rmtree(TARGET, ignore_errors=True)
        note("旧 ppt-master 已移除")

    tmp = ROOT / UNZIP_DIR
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        z.extractall(tmp)
    tops = {n.replace("\\", "/").split("/")[0] for n in names if n.strip()}
    if len(tops) != 1:
        raise SystemExit(f"压缩包结构异常：顶层有 {len(tops)} 个目录，不敢乱动，已停手。")
    inner = tmp / tops.pop()
    shutil.move(str(inner), str(TARGET))
    shutil.rmtree(tmp, ignore_errors=True)
    zip_path.unlink(missing_ok=True)
    ok(f"已放到 {TARGET}")

    if keep:
        for name in (".env", "projects"):
            src = keep / name
            if src.exists():
                dst = TARGET / name
                shutil.copytree(src, dst, dirs_exist_ok=True) if src.is_dir() else shutil.copy2(src, dst)
                ok(f"已恢复 {name}")
        shutil.rmtree(keep, ignore_errors=True)


def touch_env() -> None:
    example = TARGET / ".env.example"
    env = TARGET / ".env"
    if example.exists() and not env.exists():
        shutil.copy2(example, env)
        ok("已由 .env.example 生成 ppt-master\\.env")
        note("图片搜索可选：往里填 PEXELS_API_KEY / PIXABAY_API_KEY，不填也能跑（走 Openverse 兜底）")


def verify_wrappers() -> int:
    step("对靶：tools\\bin 里 14 个包装器的目标脚本是否到位")
    bin_dir = ROOT / "tools" / "bin"
    if not bin_dir.is_dir():
        note("没找到 tools\\bin，跳过这一步")
        return -1
    good, bad = [], []
    for cmd in sorted(bin_dir.glob("*.cmd")):
        text = cmd.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"%~dp0(.+?\.py)", text)
        if not m:
            continue
        rel = m.group(1).replace("\\", "/")
        (good if (bin_dir / rel).resolve().exists() else bad).append((cmd.name, rel))
    for name, rel in good:
        ok(f"{name:<26} -> {rel}")
    for name, rel in bad:
        warn(f"{name:<26} -> 目标缺失 {rel}")
    print()
    total = len(good) + len(bad)
    if not total:
        warn("没读到任何包装器，tools\\bin 的 .cmd 格式可能不一样，回头手点一个试试")
        return -1
    note(f"对靶结果：{total} 个包装器，{len(good)} 命中，{len(bad)} 脱靶")
    return len(bad)


def install_deps() -> None:
    step("装 ppt-master 的 Python 依赖")
    req = TARGET / "requirements.txt"
    if not req.exists():
        warn("没找到 requirements.txt，跳过")
        return
    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists():
        py, who = venv_py, "本体 .venv"
    else:
        py, who = Path(sys.executable), "当前解释器"
    note(f"用 {who}：{py}")
    note("这一步会带上 PyMuPDF（AGPL 协议，只在你本机自用，别拿去做再分发）")
    r = subprocess.run([str(py), "-m", "pip", "install", "-r", str(req)])
    if r.returncode == 0:
        ok("依赖装齐")
    else:
        warn("依赖没装全。手动补一句试试：")
        print(f'    "{py}" -m pip install -r "{req}"')


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True, description="路夜 · ppt-master 组件下载器")
    ap.add_argument("--check", action="store_true", help="只测各路源通不通，不下载")
    ap.add_argument("--force", action="store_true", help="(已是默认行为) 重下并覆盖，保留 .env 和 projects")
    ap.add_argument("--skip", action="store_true", help="已经装过就跳过，不重下")
    ap.add_argument("--no-deps", action="store_true", help="只下文件，不装依赖")
    args = ap.parse_args()

    head("路夜 · ppt-master 组件下载器")
    note(f"安装目录：{TARGET}")

    if args.check:
        return do_check()

    if installed() and args.skip:
        ok("ppt-master 已经在了，按 --skip 要求跳过下载")
        note("去掉 --skip 重跑一次 = 覆盖重装成干净版")
    else:
        if installed():
            note("ppt-master 已存在 → 覆盖重下，旧的 .env 和 projects 会留一手")
        zip_path = fetch()
        unpack(zip_path)

    if not installed():
        warn("装完了但没找到标记文件，八成是你下的 ZIP 不是完整仓库，检查一下")
        return 1

    touch_env()
    misses = verify_wrappers()
    if not args.no_deps:
        install_deps()

    head("收工")
    if misses < 0:
        warn("包装器没测成，回头手点一个 .cmd 看看能不能跑")
    elif misses:
        warn(f"有 {misses} 个包装器脱靶，往上翻看哪几个")
    else:
        ok("ppt-master 就位，包装器全命中")
    note("在 Agent 里打开这个文件夹就能做 PPT 了；材料丢进 ppt-master\\projects\\")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中途按停了，没留下半成品目录（除了临时 ZIP，重跑会自动清）。")
        sys.exit(130)
