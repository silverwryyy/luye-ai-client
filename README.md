# 路夜 Luye · v1.2

一个住在 Windows 桌面上的 AI 助手：小窗常驻、能聊天、会主动搭话，崩了还会自己留证据。

> 不是「一键即用」的成品软件，是一个文件夹式的小工具。
> 装不装、装哪些，**你自己选**。

---

## 它到底能干什么

装好之后，桌面上会出现一个圆形小窗，那就是路夜。能做的事：

- **桌面形象**：常驻小窗、不挡操作（鼠标事件穿透），大小可调（`PET_SIZE`，默认 120）
- **睡眠模式**：右键菜单手动切换；睡着后左键点击 / 双击 / 拖拽 / 摸头全部失效，防手滑；光标落在圆内按右键就醒
- **聊天**：多行输入框，3～8 行自适应，Enter 发送、Shift+Enter 换行，发完自动复位
- **8 个工具**：读写本地文件、扫目录、联网搜索、跑命令、读写 Word、调技能，聊天里直接使唤它
- **三层记忆**：L1 核心锚点每轮必带、L2 长期议题命中才带、L3 临时上下文超限才折叠；关了再开还记得你
- **主动搭话**：空闲时自己开口，也能按点提醒你
- **互动**：摸头冒爱心、番茄钟
- **崩溃留证据**：僵死、无报错也会写运行日志（只在本机，不进发布包）

具体这版修了什么、加了什么，见 [CHANGELOG.md](CHANGELOG.md)。

---

## 快速开始（必装部分）

1. 双击 **`装我.bat`**
   - 电脑上没有 Python？它会自己帮你装一个（winget → 官方安装包，两级兜底）
   - 然后建 `.venv`、装依赖（走清华镜像）、做导入自检
   - 最后**当场问你要 API Key**，粘进去就自动写好 `.env`
     - DeepSeek（**必填**）：https://platform.deepseek.com/
     - Tavily（可选，联网搜索用）：https://tavily.com/
   - 必填的没填？安装会明确报错停下，不会假装「安装完成」
2. （想手动填也可以）照 `.env.example` 复制一份 `.env`，自己填两行
3. 双击 **`启动路夜.bat`**

就这三步。启动脚本会先自检（虚拟环境在不在、依赖能不能 import、Key 填没填），有问题会在控制台里直说，不会只闪一下就没了。

**环境要求**：Windows 10/11 64 位。Python 3.10+——没装也行，`装我.bat` 会处理。

---

## 可选组件（不装也能用）

下面这两样体积大、又不是聊天必需，**没有打进压缩包**。
想要就双击 **`安装可选组件.bat`**，菜单里自己挑。

| 组件 | 体积 | 装了能干嘛 | 怎么装 |
|---|---|---|---|
| **OfficeCLI** | 约 32 MB | 细读细写 docx / xlsx / pptx，支持元素级编辑 | `安装可选组件.bat` → 选 1 |
| **ppt-master** | 约 55 MB | 用自然语言做 PPT；`tools\bin` 里 14 个 `.cmd` 包装器靠它 | `安装可选组件.bat` → 选 2 |

两个都从上游官方仓库现拉，**不随包分发**：

- OfficeCLI → https://github.com/iOfficeAI/OfficeCLI （Apache 2.0，单二进制，Windows x64）
- ppt-master → https://github.com/hugohe3/ppt-master

脚本带镜像兜底（gh-proxy），拉不下来时会打印手动下载步骤。

### ⚠️ 关于 OfficeCLI 的一条硬规矩

装完之后 **不要** 执行下面这几条：

```
officecli install
officecli skills
officecli mcp register
```

它们会往你机器上检测到的 AI 编程助手（Claude Code、Cursor、Copilot…）里塞技能文件、改它们的接线。
本项目只用 OfficeCLI 的二进制本体，**不碰**这些自动化安装动作。

---

## 依赖说明

- `requirements.txt` 里 **所有版本都钉死了**（v1.1 起）。克隆下来直接
  `pip install -r requirements.txt`，装出来的环境和作者一致，不会「随缘装到哪个版本」。
- `numpy` 单独列在文件末尾并注明用途：**只有跑 demo 脚本才需要**，主程序不用。
  那句注释是给人看的（pip 不看注释）——介意的话把那一行删掉再装。

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

---

## 目录里都是啥

```
Luye_v1.2\
├─ 装我.bat              ← 先点这个（安装）
├─ 启动路夜.bat           ← 再点这个（启动）
├─ 安装可选组件.bat        ← 想要 OfficeCLI / ppt-master 就点这个
├─ 下载PPT组件.bat         ← 只装 ppt-master 的快捷入口
├─ _auto_python.bat       ← 安装器内部调用，别单独点
├─ install_setup.py       ← 安装逻辑（装依赖 + 问 Key + 写 .env）
├─ .env.example           ← Key 模板（复制成 .env 手填也行）
├─ install_optional.py    ← 可选组件安装逻辑
├─ fetch_ppt_master.py    ← ppt-master 下载器（支持 --check / --skip）
├─ desktop_bug.py         ← 主程序
├─ memory.py              ← 三层记忆引擎（L1 锚点 / L2 议题 / L3 折叠）
├─ _selftest_memory.py    ← 记忆引擎回归自测（可选，不影响正常使用）
├─ proactive.py           ← 主动搭话
├─ boot_greet.py          ← 开机问好（可选）
├─ nightly_organize.py    ← 每晚整理日程（可选）
├─ requirements.txt
├─ CHANGELOG.md
└─ tools\                 ← 工具目录（officecli\ 按需下载）
```

---

## 这版改了什么

见 [CHANGELOG.md](CHANGELOG.md)。

---

## 里面不会有什么

这个仓库 **不含** 任何个人数据：

- `.env`（API Key）
- `history.json`（聊天记忆）
- `long_term.json`（长期议题库）
- `core_anchors.md`（自定义核心锚点；没有该文件就自动用内置模板）
- `logs/`（运行日志）
- `.venv/`（虚拟环境）

克隆下来是个干净的壳，得自己填 Key 才能聊天。

---

## 许可与致谢

- 主体代码：作者自用工具，随手开源，未指定许可，请勿商用。
- **OfficeCLI**：© iOfficeAI，Apache 2.0。本项目不随包分发其二进制，由用户按需从官方仓库下载。
- **ppt-master**：© hugohe3，同样按需下载。它的依赖链里有 **PyMuPDF（AGPL）**——本机自用没问题，别再分发出去。
