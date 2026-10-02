# Campus-Notice-WeChat-Push

把 [Campus-Notice-Classifier（school-radar）](https://github.com/MAIBAI-W/Campus-Notice-Classifier)
抓到的校园通知 **实时推送到你的微信**（微信官方 OpenClaw 通道），并支持在微信里
**查历史通知、按需重新抓取、索要附件直接发文件**。

```
school-radar（22 个通知站点抓取+分类）
        │  SQLite radar.db
        ▼
weixin-bridge（本仓库，每 15 分钟对比一次，只推新增）
        │  openclaw message send
        ▼
OpenClaw + 微信官方 openclaw-weixin 插件 ──▶ 你的微信
```

## 功能

- **只推新增**：每 15 分钟抓一轮，只推这轮新出现的通知，旧的绝不重发
- **停机补发**：关机/睡眠期间漏的通知，下次开机自动抓回并**全部**发给你
  （≥5 条时先发一条说明，再逐条发，每条间隔约 1 秒）
- **查历史**：在微信里直接对 bot 说「帮我找一下之前的补考通知」
- **要附件**：对 bot 说「把这条通知的报名表发我」，bot 下载后直接发文件给你
- **随时重抓**：对 bot 说「重新抓一下官网最新通知」
- 纯 Python 标准库实现（bridge / notice-tool 零第三方依赖），限速友好（15 分钟/轮）
- **隐私**：教务处等站点要登录时，由使用者**本人提供自己的** Cookie（Cookie 绑定本人校园账号，
  等于本人登录会话，勿用他人、勿共享）；Cookie 只存本机 `bridge-config.json`（已在 .gitignore，不进 git）

## 组成

| 文件 | 作用 |
|---|---|
| `bridge/bridge.py` | 推送桥：定时抓取 → 对比 radar.db → 只推新增 → 调 openclaw 发微信 |
| `bridge/notice-tool.py` | 按需工具：`search/show/files/fetch/refresh/set-cookie` 查历史、下载附件、触发重抓（支持 SSO Cookie 与下载验证码 `--code`） |
| `bridge/bridge-config.example.json` | 配置模板（复制为 `bridge-config.json` 后填写） |
| `bridge/start-bridge.bat` | 一键启动推送桥 |
| `skill/school-notice/SKILL.md` | OpenClaw 技能：教会 bot 查历史/发附件（装到 `~/.openclaw/workspace/skills/`） |
| `autostart/` | 启动方式模板：一键启动 `start-all.bat`；开机自启 / 桌面快捷方式 二选一 |
| `patches/` | OpenClaw Windows 兼容补丁（bot 不回消息时看这里） |

## 快速开始

### 0. 前置

- Windows + Python 3.10+
- [school-radar v1.0.0](https://github.com/MAIBAI-W/Campus-Notice-Classifier/releases/tag/v1.0.0)
  解压到 `deploy-work\school-radar-pkg\`，双击 `school-radar.exe`，工作台 `http://127.0.0.1:8765`
- [OpenClaw](https://docs.openclaw.ai/)（`npm install -g openclaw@latest --allow-scripts=openclaw`）
  + 微信官方插件（`npx -y @tencent-weixin/openclaw-weixin-cli@latest install`），
  扫码登录微信后 `openclaw channels status` 能看到你的会话

### 1. 配置推送桥

```powershell
cd bridge
python bridge.py --setup     # 部署向导：问几个问题，生成 bridge-config.json
python bridge.py --targets   # 查看你的微信会话 ID，填进 target（格式 xxx@im.wechat）
```

向导会问：radar_db / openclaw_bin / target / 推送间隔，
以及**「招聘、宣讲会、专业介绍这类信息要不要也自动推送？」**——
选 N（默认）则这类信息不自动推送（需要时在微信里问 bot 照样能查）；
选 y 则全部都推。之后可在 bridge-config.json 里改 `exclude_categories` / `exclude_keywords` 调整。

也可以手动配置：复制 `bridge-config.example.json` 为 `bridge-config.json` 后按需填写。

| 字段 | 说明 |
|---|---|
| `radar_db` | school-radar 的 `data\radar.db` 绝对路径 |
| `openclaw_bin` | openclaw 可执行文件完整路径（Windows 是 `%APPDATA%\npm\openclaw.cmd`） |
| `target` | 微信会话 ID（`python bridge.py --targets` 查） |
| `interval_minutes` | 抓取+推送间隔，默认 15 分钟（别调小，对学校站点友好） |
| `max_push_per_round` | 单轮上限；**0 = 不限（开机补发全部）** |
| `quiet_hours` | 免打扰时段，如 `[23, 7]`（期间只记不推） |
| `categories` / `min_score` | 只推指定分类/相关度（空/0 = 全推） |
| `exclude_categories` | 排除这些分类不自动推（如 `["jobstart"]` = 就业招聘类；部署向导可一键配好） |
| `exclude_keywords` | 标题含这些关键词的不自动推（如 招聘/宣讲会/专业介绍 等，向导可一键配好） |

### 2. 测试并启动

```powershell
python bridge.py --once --dry-run   # 只打印不发送
python bridge.py --once             # 真发一条试试
start-bridge.bat                    # 常驻：每 15 分钟抓取+推送
```

> 首轮会把库里已有通知记为基线、不推送（防刷屏），之后只推真正新增的。

### 3. 让 bot 会查历史 / 发附件（可选）

把 `skill/school-notice/` 复制到 `~\.openclaw\workspace\skills\school-notice\`，
并把 SKILL.md 里的路径改成你机器上的实际路径。之后在微信里直接说需求即可。

### 4. 启动方式（二选一：开机自启 / 桌面快捷方式）

**A. 桌面快捷方式（手动启动，推荐）**

1. 把 `autostart\start-all.bat` 复制到 `deploy-work\`（路径不对就改文件顶部的 `set` 行）
2. 双击 `autostart\make-desktop-shortcut.vbs`，桌面生成 `Campus-Notice-Push`
   快捷方式（可自行改名）
3. 以后每次开机后**双击它**即可一键启动 OpenClaw 网关 + school-radar + 推送桥。
   已在运行的部分会自动跳过，重复双击也安全

选这个方式时，建议把 OpenClaw 网关的开机自启也停掉（避免开机自动双开网关）：

```powershell
schtasks /Change /TN "OpenClaw Gateway" /DISABLE   # 想恢复开机自启时换成 /ENABLE
```

**B. 开机自启（登录后自动运行）**

- 推送桥：把 `autostart\weixin-bridge.vbs.example` 改好路径后复制到
  `shell:startup`（Win+R 输入 `shell:startup`），改名 `.vbs`
- school-radar：用 `autostart\make-shortcut.vbs` 在启动文件夹生成快捷方式
- OpenClaw 网关：安装时配好的登录时计划任务，无需管

> 两种方式选一种就好：都开的话，开机时会启动两遍（启动脚本会跳过已运行的，倒也不会出错）。

## 常见问题

**推送日志出现「找不到命令 'openclaw'」**
Windows 上 npm 装的 openclaw 是 `openclaw.cmd` 包装脚本（没有 `.exe`），
Python 调裸命令找不到。把 `openclaw_bin` 填成完整路径即可（bridge 每轮重读配置，不用重启）。

**bot 收到消息不回复，日志报 `DataCloneError: #<Object> could not be cloned`**
OpenClaw 2026.9.x 在 Windows 上的已知 bug（env 变量 Proxy 跨不过 worker 克隆边界），
见 [`patches/openclaw-worker-task-pool-datacloneerror.md`](patches/openclaw-worker-task-pool-datacloneerror.md)。

**收不到推送 / 日志报 `prepare failed`**
微信 bot 平台规定 bot 只能在你发起过会话后推送（推送要带入站消息签发的 context token）。
在微信里给 bot 随便发一条消息（如「你好」）刷新会话即可恢复。

## 说明与边界

- 推送内容是学校**公开**通知；对外链路 = 本机 bridge → 本机 openclaw → 微信官方后端，
  数据不经过任何第三方服务器
- 抓取节奏请保持 15 分钟/轮以上，与 school-radar「一人翻 22 个页面」的限速约定一致
- 对话模型只在你和 bot 聊天时消耗 token（如 DeepSeek），纯推送链路不走模型

## 致谢

- [MAIBAI-W/Campus-Notice-Classifier](https://github.com/MAIBAI-W/Campus-Notice-Classifier) —
  校园通知抓取 + 智能分类工作台（school-radar）
- [OpenClaw](https://docs.openclaw.ai/) 与腾讯官方 `@tencent-weixin/openclaw-weixin` 插件 —
  微信收发通道

## License

[MIT](LICENSE)
