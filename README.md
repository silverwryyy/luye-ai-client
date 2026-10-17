# 路夜 Luye · v1.1

一个住在 Windows 桌面上的 AI 助手：小窗常驻、能聊天、会主动搭话，崩了还会自己留证据。

> 不是「一键即用」的成品软件，是一个文件夹式的小工具。

---

## 快速开始（三步）

1. 双击 **`装我.bat`**
   - 电脑上没有 Python？它会自己帮你装一个（winget → 官方安装包，两级兜底）
   - 然后建 `.venv`、装依赖（走清华镜像）、检查 `.env`
2. 用记事本打开 **`example.txt`**，把 `填自己申请的对应api` 换成你自己的 Key，并把文件名改为.env
   - DeepSeek：https://platform.deepseek.com/
   - Tavily：https://tavily.com/
3. 双击 **`启动路夜.bat`**

启动脚本会先自检（虚拟环境在不在、依赖能不能 import、Key 填没填），有问题会在控制台里直说，不会只闪一下就没了。

**环境要求**：Windows 10/11 64 位。Python 3.10+——没装也行，`装我.bat` 会处理。

---

## 依赖说明

`requirements.txt` 里 **所有版本都钉死了**（v1.1 起）。克隆下来直接
`pip install -r requirements.txt`，装出来的环境和作者一致，不会「随缘装到哪个版本」。

| 包 | 钉死版本 |
|---|---|
| requests | 2.34.2 |
| psutil | 7.2.2 |
| python-dotenv | 1.2.4 |
| openai | 3.28.0 |
| winotify | 1.1.0 |
| pillow | 12.3.0 |
| python-docx | 1.2.0 |
| numpy（仅 demo 用） | 2.5.4 |

`numpy` 单独列在文件末尾并注明用途：**只有跑 `image_shape_demo.py` 才需要**，主程序不用。

---

## 崩了怎么办

v1.1 起自带崩溃遥测，出问题时先看包目录下这几个文件（都在根目录）：

| 文件 | 什么时候有 | 看它干嘛 |
|---|---|---|
| `launch_trace.log` | 每次启动 | 启动走到哪一步停的，卡在依赖还是 Key |
| `runtime_heartbeat.txt` | 运行期间不断刷新 | 里面是最后一次存活的时间戳，能算出「死于几点几分」 |
| `crash_trace.log` | 硬崩（连 Python 异常都没抛）时 | faulthandler 刷出的各线程调用栈 |
| `error.log` / `save_error.log` | 有可捕获异常 / 记忆存盘失败 | 普通报错的完整 traceback |
| `launch_error.log` | 启动自检失败 | 依赖导入失败的原始输出 |

提 issue 时把上面这几份一起贴上，比自己复述「它突然就没了」有用得多。

---

## 目录里都是啥

```
AI_Assistant\
├─ 装我.bat              ← 先点这个（安装）
├─ 启动路夜.bat           ← 再点这个（启动）
├─ _auto_python.bat       ← 安装器内部调用，别单独点
├─ install_setup.py       ← 安装逻辑（中文提示都在这儿）
├─ desktop_bug.py         ← 主程序
├─ proactive.py           ← 主动搭话
├─ boot_greet.py          ← 开机问好（可选）
├─ nightly_organize.py    ← 每晚整理日程（可选）
├─ image_shape_demo.py    ← 演示脚本，和主程序无关
├─ bug.gif / bug.png      ← 悬浮窗形象（删了会退化成纯文字）
├─ requirements.txt
├─ .env                   ← Key 填这里（占位符版）
├─ AGENTS.md              ← 工程铁律与踩坑记录
├─ CHANGELOG.md
├─ LICENSE
└─ tools\run_bg.py        ← 长任务后台执行器
```

---

## 这版改了什么

见 [CHANGELOG.md](CHANGELOG.md)。

---

## 外部组件：本包不含，需要请自装

路夜可以调用几类外部工具干重活，但它们体积大、授权各自独立，**没有打进本包**：

- **OfficeCLI** → https://github.com/iOfficeAI/OfficeCLI （Apache 2.0，单二进制，Windows x64）
- **ppt-master** → https://github.com/hugohe3/ppt-master
- **Skills 库**（TDD / code-review / diagnosing-bugs 等）→ `skills\skills\`

想用就自己装好放到 `tools\`（或对应目录），并在 `desktop_bug.py` 的 `_RUN_ALLOW_FIRST` 白名单里登记可执行名，详见 `AGENTS.md`。

**三个都不装也能用**：对话、联网搜索、扫目录、读文件、写文件、读写 Word 全部照常。

### ⚠️ 关于 OfficeCLI 的一条硬规矩

装完之后 **不要** 执行 `officecli install` / `officecli skills` / `officecli mcp register`——
它们会往你机器上检测到的 AI 编程助手（Claude Code、Cursor、Copilot…）里塞技能文件、改接线。

---

## 里面不会有什么

这个仓库 **不含** 任何个人数据：

- `.env` 里的真实 Key（只有占位符）
- `history.json`（聊天记忆）
- `logs/`、`*_trace.log`（运行日志）
- `.venv/`（虚拟环境）

克隆下来是个干净的壳，得自己填 Key 才能聊天。

---

## 许可证

本项目采用 **MIT License** 开源，完整条款见 [LICENSE](LICENSE)。

简单说：你可以自由使用、修改、分发，甚至商用，只需保留版权声明和许可证声明。

**第三方组件**（OfficeCLI / ppt-master / Skills 库）不随包分发，各自遵循其原有许可证
（如 Apache 2.0 / AGPL 等），使用前请自行阅读。特别提醒：ppt-master 的依赖链里有
**PyMuPDF（AGPL）**——自己机器上用没问题，别再打包分发出去。
