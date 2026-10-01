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
}


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
        out.append(dict(r))
    out.sort(key=lambda x: x.get("score") or 0, reverse=True)
    return out


# ------------------------------------------------------------------ 推送到微信

def format_message(it: dict) -> str:
    lines = [f"📢 {it.get('title') or '(无标题)'}"]
    tags = [x for x in (it.get("category_name"), it.get("doc_type"), it.get("source_name")) if x]
    if tags:
        lines.append("🏷 " + " · ".join(tags))
    meta = []
    if it.get("pub_date"):
        meta.append(f"📅 {it['pub_date']}")
    if it.get("deadline"):
        meta.append(f"⏰ 截止 {it['deadline']}")
    if it.get("audience"):
        meta.append(f"👥 {it['audience']}")
    if meta:
        lines.append("  ".join(meta))
    digest = (it.get("digest") or "").strip()
    if digest:
        lines.append(digest[:300] + ("…" if len(digest) > 300 else ""))
    if it.get("url"):
        lines.append(f"🔗 {it['url']}")
    return "\n".join(lines)


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


def send_wechat(cfg: dict, text: str) -> tuple[bool, str]:
    """调 openclaw message send 发到微信。返回 (是否成功, 输出摘要)。"""
    target = (cfg.get("target") or "").strip()
    if not target:
        return False, "未配置 target（bridge-config.json）"
    cmd = [
        resolve_openclaw(cfg),
        "message", "send",
        "--channel", cfg.get("channel") or "openclaw-weixin",
        "--target", target,
        "--message", text,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False, f"找不到命令 {cmd[0]!r}，请在 bridge-config.json 里配置 openclaw_bin 完整路径"
    except subprocess.TimeoutExpired:
        return False, "openclaw message send 超时(60s)"
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0:
        return True, out.strip()[:300]
    # 微信 bot 平台要求：会话须由你先发一条消息开启（context token 随入站消息签发）。
    # 报 "prepare failed" 时提示用户去微信里给 bot 发条消息刷新会话。
    if "prepare failed" in out:
        out += " | 提示: 请在微信里给本 bot 随便发一条消息（如“你好”），刷新推送会话后即可恢复"
    return False, out.strip()[:400]


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
    first_round = not seen and not state.get("started_once")
    push_history = bool(cfg.get("push_history_on_start"))

    # 首轮且不推历史：把库里已有的全部记为基线，之后只推真正新增的，防止刷屏
    if first_round and not push_history:
        existing = fetch_new_items(cfg["radar_db"], set(), cfg)
        seen.update(it["url"] for it in existing)
        state["sent_urls"] = sorted(seen)
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
        ok, out = send_wechat(cfg, lead)
        if not ok:
            log(f"补发说明推送失败: {out}")

    ok_count = 0
    for i, it in enumerate(batch):
        text = format_message(it)
        if dry_run:
            log(f"[dry-run] 将推送:\n{text}\n")
            ok_count += 1
        elif quiet:
            log(f"免打扰时段，跳过推送: {it.get('title')}")
            continue
        else:
            ok, out = send_wechat(cfg, text)
            if ok:
                ok_count += 1
                log(f"已推送: {it.get('title')}")
                if i < len(batch) - 1:
                    time.sleep(gap)
            else:
                log(f"推送失败: {it.get('title')} → {out}")
                # 失败不记入 sent，下一轮重试
                continue
        if not dry_run:
            seen.add(it["url"])

    state["sent_urls"] = sorted(seen)
    state["started_once"] = True
    state["last_round"] = datetime.now().isoformat(timespec="seconds")
    save_json(STATE_PATH, state)
    log(f"本轮完成：成功 {ok_count}/{len(batch)}")
    return "ok"


def list_targets(cfg: dict) -> None:
    """帮用户找 target：列出 openclaw channels 状态。"""
    cmd = [resolve_openclaw(cfg), "channels", "status"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
        print(r.stdout or r.stderr)
    except Exception as e:
        print(f"执行失败: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="school-radar → 微信推送桥")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--dry-run", action="store_true", help="不真正发送，只打印")
    ap.add_argument("--reset-state", action="store_true", help="清空已推送记录")
    ap.add_argument("--targets", action="store_true", help="显示 openclaw 渠道状态（找 target 用）")
    args = ap.parse_args()

    cfg = load_config()
    if args.targets:
        list_targets(cfg)
        return 0

    if args.reset_state and STATE_PATH.exists():
        STATE_PATH.unlink()
        log("已清空推送记录")

    state = load_json(STATE_PATH, {})

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
