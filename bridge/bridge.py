#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weixin-bridge — school-radar → 微信 推送桥

职责：
  1. 周期性触发 school-radar 的 /api/refresh 抓取学校通知
  2. 对比 radar.db 找出新出现的通知（自动推送只发新增，不发旧的）
  3. 通过 `openclaw message send`（微信官方通道）推送到你的微信
  4. 开机/停机后首次抓到的漏发通知会全部补发（不限条数，带补发说明）

只用标准库，无第三方依赖。数据只在本机流转；
推送目标是你自己配置的微信会话（见 bridge-config.json）。

用法：
  python bridge.py               # 正式运行
  python bridge.py --once        # 只跑一轮（测试）
  python bridge.py --dry-run     # 跑一轮但不真正发消息，只打印
  python bridge.py --reset-state # 清空"已推送"记录（下次会把现有通知全推一遍）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "bridge-config.json"
STATE_PATH = BASE_DIR / "sent-state.json"
LOG_PATH = BASE_DIR / "bridge.log"

DEFAULT_CONFIG = {
    # school-radar 工作台地址（school-radar-cli.exe 启动后打印的地址）
    "radar_base": "http://127.0.0.1:8765",
    # radar.db 完整路径（school-radar 的 data 目录里）
    "radar_db": "",
    # openclaw 可执行文件；装在非 PATH 位置时填完整路径
    "openclaw_bin": "openclaw",
    # 微信通道名（openclaw-weixin 插件注册的渠道名）
    "channel": "openclaw-weixin",
    # 推送目标（登录微信后用 `openclaw channels status` 或发一条测试消息确认）
    # 一般填自己的微信号会话，如 "user:wxid_xxx"；留空则首次运行时打印候选列表
    "target": "",
    # 抓取+推送间隔（分钟）。22 个站点抓一轮要 1-3 分钟，别设太小
    "interval_minutes": 15,
    # 只推相关度不低于该分数的通知（0 = 全推）
    "min_score": 0.0,
    # 只推这些分类（空 = 全部）。分类 id 例如 jobstart/makeup/money/gongshi
    "categories": [],
    # 免打扰时段 [起, 止]（24 小时制），此期间只记不推；空 = 全天可推
    "quiet_hours": [],
    # 首次运行是否把库里已有的通知也推一遍（false = 只推启动后新抓到的）
    "push_history_on_start": False,
    # 单轮最多推送条数。0 = 不限（开机/停机后把漏掉的全部补发）；>0 时截断
    "max_push_per_round": 0,
    # 补发多条时的单条间隔（秒），避免发太快
    "send_interval_seconds": 1.2,
    # 不自动推送这些分类（如 jobstart = 就业招聘）；空 = 不排除。要查这类信息随时问 bot
    "exclude_categories": [],
    # 标题含这些关键词的不自动推送（招聘、专业介绍等噪声）；空 = 不排除
    "exclude_keywords": [],
    # 等 openclaw 发送回执的超时（秒）。超时多半其实已送达——按已推送标记，防止下轮重发轰炸
    "send_timeout_seconds": 90,
}

# 招聘/宣讲会/专业介绍这类信息的默认过滤规则。
# 部署向导（--setup）会问要不要也自动推送；选“不推”就把下面写进配置。
NOISE_CATEGORIES = ["jobstart"]
NOISE_KEYWORDS = [
    "招聘", "招募", "校园大使", "pick你的专业", "专业吧", "专业介绍",
    "宣讲会", "双选会", "就业指导", "考公", "公考", "事业单位",
]


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_PATH, {}) or {})
    return cfg


def clear_pause_marker(cfg: dict) -> None:
    """启动时清除临时停推标记 __paused_until_restart__。

    更新代码时旧桥进程还在跑、手里的过滤规则是老的，会用老格式把通知推出来；
    所以在 bridge-config.json 里把 categories 置为该标记让旧进程闭嘴，
    新进程一启动就把它清掉恢复正常推送。"""
    if "__paused_until_restart__" not in (cfg.get("categories") or []):
        return
    raw = load_json(CONFIG_PATH, {}) or {}
    raw["categories"] = [c for c in (raw.get("categories") or []) if c != "__paused_until_restart__"]
    save_json(CONFIG_PATH, raw)
    cfg["categories"] = list(raw["categories"])
    log("已清除临时停推标记，恢复正常推送")


# ---------------------------------------------------------------- school-radar

def api(base: str, path: str, timeout: float = 30.0) -> dict:
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def radar_alive(base: str) -> bool:
    try:
        api(base, "/api/alive", timeout=3)
        return True
    except Exception:
        return False


def trigger_crawl(base: str) -> bool:
    """触发一次抓取。已有抓取在跑时服务端自己会忽略。"""
    try:
        r = api(base, "/api/refresh", timeout=10)
        return bool(r.get("started")) or True
    except Exception as e:
        log(f"触发抓取失败: {e}")
        return False


def wait_crawl_done(base: str, budget_seconds: int = 360) -> bool:
    """等服务端抓取结束（服务端在 /api/items 里返回 crawling 标志）。"""
    deadline = time.time() + budget_seconds
    while time.time() < deadline:
        try:
            d = api(base, "/api/items?days=1", timeout=15)
            if not d.get("crawling"):
                return True
        except Exception:
            pass
        time.sleep(5)
    log("等待抓取超时，按现有数据继续")
    return False


def fetch_new_items(db_path: str, seen_urls: set, cfg: dict) -> list[dict]:
    """从 radar.db 取出还没推送过的通知，按分数从高到低。"""
    p = Path(db_path)
    if not p.exists():
        log(f"radar.db 不存在: {p}")
        return []
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT url, title, digest, source_name, pub_date, deadline, category, "
            "category_name, doc_type, audience, score, first_seen "
            "FROM items ORDER BY COALESCE(first_seen, pub_date, '') DESC LIMIT 2000"
        ).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        url = r["url"]
        if not url or url in seen_urls:
            continue
        score = r["score"] or 0.0
        if score < float(cfg.get("min_score") or 0):
            continue
        cats = cfg.get("categories") or []
        if cats and (r["category"] or "") not in cats:
            continue
        ex_cats = set(cfg.get("exclude_categories") or [])
        if (r["category"] or "") in ex_cats:
            continue
        title = (r["title"] or "")
        ex_kw = [k for k in (cfg.get("exclude_keywords") or []) if k]
        if any(k in title for k in ex_kw):
            continue
        out.append(dict(r))
    out.sort(key=lambda x: x.get("score") or 0, reverse=True)
    return out


# ------------------------------------------------------------------ 推送到微信

def format_message(it: dict) -> str:
    # openclaw.cmd 经 cmd.exe 传多行 --message 时换行会被截断（只剩第一行），
    # 所以整条消息压成单行，链接一定带上，形如：
    # 📢 标题 ｜ 🏷 标签 ｜ 📅 日期 ｜ 🔗 链接
    parts = [f"📢 {it.get('title') or '(无标题)'}"]
    tags = [x for x in (it.get("category_name"), it.get("doc_type"), it.get("source_name")) if x]
    if tags:
        parts.append("🏷 " + " · ".join(tags))
    meta = []
    if it.get("pub_date"):
        meta.append(f"📅 {it['pub_date']}")
    if it.get("deadline"):
        meta.append(f"⏰ 截止 {it['deadline']}")
    if it.get("audience"):
        meta.append(f"👥 {it['audience']}")
    if meta:
        parts.append("  ".join(meta))
    digest = (it.get("digest") or "").strip().replace("\n", " ")
    if digest:
        parts.append(digest[:120] + ("…" if len(digest) > 120 else ""))
    if it.get("url"):
        parts.append(f"🔗 {it['url']}")
    return " ｜ ".join(parts)


def in_quiet_hours(quiet: list) -> bool:
    if not quiet or len(quiet) != 2:
        return False
    try:
        now = datetime.now().hour + datetime.now().minute / 60.0
        start, end = float(quiet[0]), float(quiet[1])
    except (TypeError, ValueError):
        return False
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # 跨午夜，如 [23, 7]


def resolve_openclaw(cfg: dict) -> str:
    """定位 openclaw 可执行文件。

    Windows 上 npm 装的 openclaw 是 .cmd 包装脚本（没有 .exe），
    subprocess 裸命令找不到；开机自启的进程 PATH 里还常缺 npm 目录。
    所以：绝对路径直接用 → PATH 里找 → 兜底 %APPDATA%\\npm\\openclaw.cmd。
    """
    name = (cfg.get("openclaw_bin") or "openclaw").strip().strip('"')
    if Path(name).is_absolute() or (len(name) > 2 and name[1] == ":"):
        return name
    found = shutil.which(name)
    if found:
        return found
    npm_dir = Path(os.environ.get("APPDATA") or "") / "npm"
    for cand in (f"{name}.cmd", f"{name}.exe", f"{name}.bat", name):
        p = npm_dir / cand
        if p.exists():
            return str(p)
    return name


def send_wechat(cfg: dict, text: str) -> tuple[str, str]:
    """调 openclaw message send 发到微信。

    返回 (状态, 输出摘要)，状态取值：
      ok      —— 发送成功
      timeout —— 等回执超时，消息多半其实已经发出去了；
                 当作已送达处理（见 run_round），避免下一轮重发造成重复推送
      fail    —— 明确失败，下一轮可以重试
    """
    target = (cfg.get("target") or "").strip()
    if not target:
        return "fail", "未配置 target（bridge-config.json）"
    limit = float(cfg.get("send_timeout_seconds") or 90)
    cmd = [
        resolve_openclaw(cfg),
        "message", "send",
        "--channel", cfg.get("channel") or "openclaw-weixin",
        "--target", target,
        "--message", text,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=limit, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return "fail", f"找不到命令 {cmd[0]!r}，请在 bridge-config.json 里配置 openclaw_bin 完整路径"
    except subprocess.TimeoutExpired:
        return "timeout", f"openclaw message send 超时({int(limit)}s)"
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0:
        return "ok", out.strip()[:300]
    # 微信 bot 平台要求：会话须由你先发一条消息开启（context token 随入站消息签发）。
    # 报 "prepare failed" 时提示用户去微信里给 bot 发条消息刷新会话。
    if "prepare failed" in out:
        out += " | 提示: 请在微信里给本 bot 随便发一条消息（如“你好”），刷新推送会话后即可恢复"
    return "fail", out.strip()[:400]


# ---------------------------------------------------------------------- 主循环

def run_round(cfg: dict, state: dict, dry_run: bool) -> str:
    """跑一轮。返回 "radar_down" 表示 school-radar 不在线（主循环用短间隔重试）。"""
    base = cfg["radar_base"]
    if not radar_alive(base):
        log(f"school-radar 没在跑（{base} 无响应）。请先启动 school-radar-cli.exe 或 school-radar.exe；稍后自动重试")
        return "radar_down"

    log("触发抓取…")
    trigger_crawl(base)
    wait_crawl_done(base)

    seen = set(state.get("sent_urls") or [])
    sent_titles = set(state.get("sent_titles") or [])
    first_round = not seen and not state.get("started_once")
    push_history = bool(cfg.get("push_history_on_start"))

    # 首轮且不推历史：把库里已有的全部记为基线，之后只推真正新增的，防止刷屏
    if first_round and not push_history:
        existing = fetch_new_items(cfg["radar_db"], set(), cfg)
        seen.update(it["url"] for it in existing)
        sent_titles.update((it.get("title") or "").strip() for it in existing if (it.get("title") or "").strip())
        state["sent_urls"] = sorted(seen)
        state["sent_titles"] = sorted(sent_titles)
        state["started_once"] = True
        state["last_round"] = datetime.now().isoformat(timespec="seconds")
        save_json(STATE_PATH, state)
        log(f"建立基线：现有 {len(existing)} 条通知记为已读不推送；此后只推新抓到的。"
            f"（想把历史高分通知也推一遍：把 push_history_on_start 改 true 后跑 "
            f"python bridge.py --reset-state --once）")
        return "ok"

    items = fetch_new_items(cfg["radar_db"], seen, cfg)

    if not items:
        log("没有新通知")
        state["started_once"] = True
        state["last_round"] = datetime.now().isoformat(timespec="seconds")
        save_json(STATE_PATH, state)
        return "ok"

    limit = int(cfg.get("max_push_per_round") or 0)
    batch = items if limit <= 0 else items[:limit]
    log(f"发现 {len(items)} 条新通知，本轮推送 {len(batch)} 条"
        + (f"（上限 {limit}，剩余下轮继续）" if limit > 0 and len(items) > limit else ""))

    quiet = in_quiet_hours(cfg.get("quiet_hours") or [])
    gap = float(cfg.get("send_interval_seconds") or 1.2)

    # 一次补发多条（如开机后补齐停机期间的）：先发一条说明再逐条发
    if not dry_run and not quiet and len(batch) >= 5:
        lead = f"📮 有 {len(batch)} 条新通知（含停机期间补发），现在逐条发给你："
        st, out = send_wechat(cfg, lead)
        if st == "fail":
            log(f"补发说明推送失败: {out}")

    ok_count = 0
    for i, it in enumerate(batch):
        text = format_message(it)
        title = it.get("title") or "(无标题)"
        # 标题级去重：同一标题只推一次。抓取超时重试等场景会换 URL 复现同一条通知，
        # 之前正是它造成重复推送（银行招聘那条发了两遍）
        if title in sent_titles:
            log(f"跳过（同标题已推送过）: {title}")
            if not dry_run:
                seen.add(it["url"])
            continue
        if dry_run:
            log(f"[dry-run] 将推送: {text}")
            ok_count += 1
        elif quiet:
            log(f"免打扰时段，跳过推送: {title}")
            continue
        else:
            st, out = send_wechat(cfg, text)
            if st == "ok":
                ok_count += 1
                log(f"已推送: {title}")
                if i < len(batch) - 1:
                    time.sleep(gap)
            elif st == "timeout":
                # 超时≠没送到。openclaw 发出去只是回执慢，多数情况消息已经到了；
                # 此时必须记为已推送，否则下一轮重发就是重复轰炸。
                # 万一真丢了，按需时说一声即可补查。
                ok_count += 1
                log(f"推送超时（按已送达处理，防重复）: {title}")
            else:
                log(f"推送失败: {title} → {out}")
                # 失败不记入 sent，下一轮重试
                continue
        if not dry_run:
            seen.add(it["url"])
            sent_titles.add(title)

    state["sent_urls"] = sorted(seen)
    state["sent_titles"] = sorted(sent_titles)
    state["started_once"] = True
    state["last_round"] = datetime.now().isoformat(timespec="seconds")
    save_json(STATE_PATH, state)
    log(f"本轮完成：成功 {ok_count}/{len(batch)}")
    return "ok"


def backfill_titles(state: dict, cfg: dict) -> None:
    """老状态只有 sent_urls 没有 sent_titles，从 radar.db 把已推 URL 的标题补回来。
    否则同一条旧通知换个 URL 再出现时，标题去重拦不住它。"""
    urls = state.get("sent_urls") or []
    titles = set(state.get("sent_titles") or [])
    if not urls or not cfg.get("radar_db"):
        return
    p = Path(cfg["radar_db"])
    if not p.exists():
        return
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT url, title FROM items").fetchall()
    finally:
        conn.close()
    want = set(urls)
    changed = False
    for url, title in rows:
        t = (title or "").strip()
        if url in want and t and t not in titles:
            titles.add(t)
            changed = True
    if changed:
        state["sent_titles"] = sorted(titles)
        save_json(STATE_PATH, state)
        log(f"标题去重已回填：共 {len(titles)} 个标题")


def cmd_setup() -> int:
    """部署向导：交互式生成 bridge-config.json（首次部署跑一次）。

    会问几个问题（路径、target、要不要自动推送招聘/专业介绍等噪声），
    答完直接写配置文件，之后正常启动即可。"""
    if CONFIG_PATH.exists():
        ans = input(f"{CONFIG_PATH} 已存在，覆盖重配？(y/N): ").strip().lower()
        if ans != "y":
            print("已取消，配置未改动。")
            return 0

    print("=== weixin-bridge 部署向导 ===")
    print("直接回车用括号里的默认值。\n")

    cfg = dict(DEFAULT_CONFIG)

    v = input("radar.db 完整路径: ").strip()
    if v:
        cfg["radar_db"] = v

    v = input(f"openclaw 可执行文件 ({cfg['openclaw_bin']}): ").strip()
    if v:
        cfg["openclaw_bin"] = v

    v = input("微信会话 target（python bridge.py --targets 可查，如 xxx@im.wechat）: ").strip()
    if v:
        cfg["target"] = v

    v = input(f"抓取+推送间隔分钟 ({cfg['interval_minutes']}): ").strip()
    if v:
        try:
            cfg["interval_minutes"] = max(3, int(v))
        except ValueError:
            pass

    print("\n以下信息要不要自动推送？")
    print("  - 招聘/宣讲会/双选会/就业指导/考公类")
    print("  - 专业介绍/专业选择类")
    v = input("也推给我 (y/N，选 N 则这类信息不自动推送，需要时问 bot): ").strip().lower()
    if v != "y":
        cfg["exclude_categories"] = list(NOISE_CATEGORIES)
        cfg["exclude_keywords"] = list(NOISE_KEYWORDS)
        print("→ 已过滤这类信息（bridge-config.json 里可随时改 exclude_*）")
    else:
        print("→ 全部都推（可在 bridge-config.json 里配 exclude_* 过滤）")

    save_json(CONFIG_PATH, cfg)
    print(f"\n已写入 {CONFIG_PATH}")
    print("下一步：python bridge.py --targets 核对 target，然后 python bridge.py --once --dry-run 试跑")
    return 0


def list_targets(cfg: dict) -> None:
    """帮用户找 target：列出 openclaw channels 状态。"""
    cmd = [resolve_openclaw(cfg), "channels", "status"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
    except Exception as e:
        print(f"执行失败: {e}")


def main() -> int:
    # Windows 控制台默认 GBK，打印 emoji 会崩；统一改 UTF-8 输出
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="school-radar → 微信推送桥")
    ap.add_argument("--setup", action="store_true", help="首次部署向导（生成 bridge-config.json）")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--dry-run", action="store_true", help="不真正发送，只打印")
    ap.add_argument("--reset-state", action="store_true", help="清空已推送记录")
    ap.add_argument("--targets", action="store_true", help="显示 openclaw 渠道状态（找 target 用）")
    args = ap.parse_args()

    if args.setup:
        return cmd_setup()

    cfg = load_config()
    clear_pause_marker(cfg)
    if args.targets:
        list_targets(cfg)
        return 0

    if args.reset_state and STATE_PATH.exists():
        STATE_PATH.unlink()
        log("已清空推送记录")

    state = load_json(STATE_PATH, {})
    backfill_titles(state, cfg)

    if not cfg.get("radar_db"):
        log("bridge-config.json 里 radar_db 还没填（school-radar 的 data\\radar.db 路径）")
        return 2
    if not cfg.get("target") and not args.dry_run:
        log("bridge-config.json 里 target 还没填。先跑:  python bridge.py --targets  看看渠道状态")
        return 2

    interval = max(3, int(cfg.get("interval_minutes") or 15)) * 60

    if args.once:
        run_round(cfg, state, args.dry_run)
        return 0

    log(f"推送桥启动：每 {interval // 60} 分钟抓取+推送一轮（只推新增；停机漏掉的开机后自动补发）。Ctrl+C 退出。")
    while True:
        status = "ok"
        try:
            cfg = load_config()  # 每轮重读配置，改完 bridge-config.json 不用重启
            status = run_round(cfg, state, args.dry_run)
        except KeyboardInterrupt:
            log("收到退出信号，bye")
            return 0
        except Exception as e:
            log(f"本轮出错（下轮继续）: {e}")
        try:
            # school-radar 不在线时快一点重试，别干等一个完整周期
            time.sleep(60 if status == "radar_down" else interval)
        except KeyboardInterrupt:
            log("收到退出信号，bye")
            return 0


if __name__ == "__main__":
    sys.exit(main())
