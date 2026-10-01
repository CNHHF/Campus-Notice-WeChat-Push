---
name: school-notice
description: "校园通知助手：查询 school-radar 历史通知、按需重新抓取学校官网、下载通知附件并发送给用户。当用户问到 查找/搜索/之前/历史 的学校通知、索要 报名表/附件/表格/文件、或要某条通知的详情时使用。"
---

# 校园通知（school-radar）

本机跑着 school-radar（浙江财经大学东方学院 22 个站点的通知抓取+分类）。
用下面的 CLI 查询历史、下载附件。**所有命令用绝对路径，Windows 环境。**

工具位置：`C:\Users\YOURNAME\deploy-work\weixin-bridge\notice-tool.py`
（命令里 `python` 不可用时改用 `C:\Program Files\Python312\python.exe`）

## 1. 查找历史通知

用户想找之前的/某主题的通知时：

```bash
python C:/Users/YOURNAME/deploy-work/weixin-bridge/notice-tool.py search 关键词1 关键词2 --limit 20
```

- 多个关键词是 AND 关系；也可加 `--days 365`（只看一年内）、`--category contest`
- 分类 id：jobstart 就业 / examtgt 考研考公 / makeup 补考选课 / money 评奖评优 /
  gongshi 公示 / venue 场地采购 / contest 竞赛讲座 / teachrun 教学运行 /
  research 教研 / teacherhire 教师招聘 / life 生活服务
- 输出是按时间倒序的列表（标题/日期/分类/链接）。把最相关的几条用简洁中文转述给用户，
  附上链接（🔗 行的 URL）。

如果用户要求"查最新的/重新抓一下/现在去官网看"，先触发全量抓取再搜索：

```bash
python C:/Users/YOURNAME/deploy-work/weixin-bridge/notice-tool.py refresh
```

（抓一轮要 1-3 分钟，命令会自动等完成。）

## 2. 看某条通知详情

```bash
python C:/Users/YOURNAME/deploy-work/weixin-bridge/notice-tool.py show 通知URL
```

## 3. 用户索要附件（报名表/表格/文件）

**下载后必须用 message 工具把文件发给用户**，不要只给链接：

```bash
python C:/Users/YOURNAME/deploy-work/weixin-bridge/notice-tool.py fetch 通知URL
```

命令会打印每个附件的绝对保存路径（在 `C:\Users\YOURNAME\deploy-work\attachments\` 下）。
然后立刻用 message 工具发送：

- `action='send'`，`media` 设为上面打印的**绝对路径**（不要相对路径）
- 一条消息发一个文件；有多个附件就发多次
- 可在第一条前加一句简短说明（附件名是什么、来自哪条通知）

如果 fetch 报"没有发现附件"，把通知里的说明（如"发送至邮箱 xxx"）转告用户。

### 教务处等站点要登录（SSO）怎么办

`jw.zufedfc.edu.cn`（教务处）在统一身份认证后面，匿名抓到的是登录页，所以 files/fetch 会
打印"⚠️ 该页面需要校园统一身份认证…"并以退出码 3 结束。此时**不要**说"没有附件"，
按下面流程走：

1. 把上面的登录提示（F12 取 Cookie 的步骤）转告用户，请他把**自己**浏览器里的 Cookie 整段发来。
   ⚠️ Cookie 绑定本人校园账号（等于他本人的登录会话），必须由提问者自己发自己的；
   不要用别人的 Cookie、不要代收转发、不要写进示例配置。
2. 收到 Cookie 后保存（只存在本机 bridge-config.json）：

   ```bash
   python C:/Users/YOURNAME/deploy-work/weixin-bridge/notice-tool.py set-cookie jw.zufedfc.edu.cn "用户发来的Cookie整段"
   ```

3. 重新 `fetch` 同一 URL，成功后照常把附件绝对路径用 message 工具发给用户。
4. Cookie 过期时（又报登录页/退出码 3），再向用户要一次新的即可。

### 下载要验证码怎么办

教务处的附件下载链接（download.jsp）会先弹"请输入验证码下载附件"。fetch 遇到时
退出码 4，把验证码图片存到通知目录（输出里有「验证码图片:」路径）。此时：

1. 用 message 工具把**验证码图片**发给用户，请他回复那 4 个字符。
2. 收到后重试（--code 拼到下载链接上）：

   ```bash
   python C:/Users/YOURNAME/deploy-work/weixin-bridge/notice-tool.py fetch 通知URL --code 用户回复的验证码
   ```

3. 成功后照常把附件发给用户。验证码是一次性的，下次下载可能再要一次，重复本流程。

## 4. 与自动推送的关系

- 微信里收到的 📢 通知推送是桥自动发的，只发**新增**通知；停机漏掉的开机后自动补发。
- 用户对某条推送感兴趣、问"这是什么/详情/有附件吗"时：用 push 里的 🔗 URL 走 show / files / fetch。

## 边界

- 只查本机雷达库和学校**公开**页面；不要抓与通知无关的站点，附件单个超过 50MB 就放弃并告知用户。
- 查库用 search（本地，快）；只有 refresh/fetch 才会访问学校网站。
