#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notice-tool — school-radar 通知查询 / 附件下载工具

给微信 bot（school-bot）按需使用，也可手动运行。只用标准库。

子命令：
  search <关键词...>   搜索历史通知（标题/摘要/正文）
  show <通知URL>       查看一条通知详情
  files <通知URL>      列出该通知页面上的附件链接（实时抓页面）
  fetch <通知URL>      下载附件到本地目录，打印保存路径（供 bot 发送）
  refresh              触发一次全量抓取（电脑错过的通知也会抓回来）

用法示例：
  python notice-tool.py search 补考 --days 365
  python notice-tool.py search 竞赛 招募 --category contest
  python notice-tool.py fetch https://gs.zufedfc.edu.cn/info/1022/3951.htm
  python notice-tool.py refresh
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import ssl
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "bridge-config.json"
DEFAULT_DB = r"C:/Users/YOURNAME/deploy-work/school-radar-pkg/school-radar/data/radar.db"
DEFAULT_OUT = r"C:/Users/YOURNAME/deploy-work/attachments"
RADAR_BASE = "http://127.0.0.1:8765"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) school-radar-notice-tool/1.0 "
      "(student notice fetcher; contact: local user)")
FILE_EXT = re.compile(
    r"\.(docx?|xlsx?|pptx?|pdf|zip|rar|7z|txt|wps|jpg|jpeg|png|gif|mp4|mp3|csv)(\?|$)",
    re.I,
)


# ---------------------------------------------------------------- 基础工具

def load_db_path() -> Path:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        p = cfg.get("radar_db")
        if p:
            return Path(p)
    except (OSError, json.JSONDecodeError):
        pass
    return Path(DEFAULT_DB)


def connect_db() -> sqlite3.Connection:
    p = load_db_path()
    if not p.exists():
        print(f"radar.db 不存在: {p}", file=sys.stderr)
        sys.exit(2)
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def http_get(url: str, timeout: float = 30.0):
    """GET，返回 (bytes, final_url, headers)。证书失败时退回不校验（公开站点）。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    ctx = ssl.create_default_context()
    try:
        r = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    except (urllib.error.URLError, ssl.SSLError):
        ctx = ssl._create_unverified_context()
        r = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    try:
        return r.read(), r.geturl(), r.headers
    finally:
        r.close()


def decode_html(data: bytes, headers) -> str:
    charset = None
    if headers is not None:
        ctype = headers.get("Content-Type") or ""
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        if m:
            charset = m.group(1)
    if not charset:
        m = re.search(rb"charset=[\"']?([\w-]+)", data[:4000], re.I)
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    for enc in (charset, "utf-8", "gb18030"):
        if not enc:
            continue
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", name).strip()
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    name = name.strip(" .")
    return name[:120] or "attachment"


def row_to_item(r: sqlite3.Row) -> dict:
    return {
        "url": r["url"],
        "title": r["title"],
        "pub_date": r["pub_date"],
        "deadline": r["deadline"],
        "category": r["category"],
        "category_name": r["category_name"],
        "doc_type": r["doc_type"],
        "source_name": r["source_name"],
        "audience": r["audience"],
        "files": r["files"],
        "digest": r["digest"],
        "body": r["body"],
    }


def print_item_brief(it: dict, idx: int | None = None) -> None:
    prefix = f"{idx}. " if idx is not None else ""
    date = it.get("pub_date") or "?"
    cat = it.get("category_name") or it.get("category") or "?"
    dl = f"  ⏰截止 {it['deadline']}" if it.get("deadline") else ""
    files = f"  📎有附件" if it.get("files") else ""
    print(f"{prefix}[{date}] {it.get('title') or '(无标题)'}")
    print(f"   🏷 {cat} · {it.get('source_name') or '?'}{dl}{files}")
    print(f"   🔗 {it['url']}")


# ---------------------------------------------------------------- search / show

def cmd_search(args) -> int:
    kws = [k.strip() for k in args.keywords if k.strip()]
    if not kws:
        print("请给出至少一个关键词", file=sys.stderr)
        return 2
    conn = connect_db()
    try:
        sql = ("SELECT url, title, pub_date, deadline, category, category_name, "
               "doc_type, source_name, audience, files, digest, body FROM items WHERE 1=1")
        params: list = []
        for kw in kws:
            sql += " AND (title LIKE ? OR digest LIKE ? OR body LIKE ?)"
            like = f"%{kw}%"
            params += [like, like, like]
        if args.category:
            sql += " AND category = ?"
            params.append(args.category)
        if args.days and args.days > 0:
            cutoff = datetime.now().timestamp() - args.days * 86400
            cutoff_s = datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d")
            sql += " AND COALESCE(pub_date, first_seen, '') >= ?"
            params.append(cutoff_s)
        sql += " ORDER BY COALESCE(pub_date, first_seen, '') DESC LIMIT ?"
        params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
        items = [row_to_item(r) for r in rows]

        if args.json:
            for it in items:
                it.pop("body", None)
            print(json.dumps(items, ensure_ascii=False, indent=2))
            return 0

        if not items:
            print(f"没有找到匹配的通知（关键词: {' '.join(kws)}）")
            return 0
        print(f"找到 {len(items)} 条（按发布时间倒序）：\n")
        for i, it in enumerate(items, 1):
            print_item_brief(it, i)
            print()
        return 0
    finally:
        conn.close()


def find_item(conn: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    r = conn.execute("SELECT * FROM items WHERE url = ?", (url,)).fetchone()
    if r:
        return r
    like = f"%{url}%"
    return conn.execute(
        "SELECT * FROM items WHERE url LIKE ? ORDER BY length(url) LIMIT 1", (like,)
    ).fetchone()


def cmd_show(args) -> int:
    conn = connect_db()
    try:
        r = find_item(conn, args.url)
        if not r:
            print(f"库里没有这条通知: {args.url}", file=sys.stderr)
            return 1
        it = dict(r)
        if args.json:
            print(json.dumps(it, ensure_ascii=False, indent=2))
            return 0
        print(f"📌 {it.get('title')}")
        print(f"🏷 {it.get('category_name')} · {it.get('doc_type')} · {it.get('source_name')}")
        meta = []
        if it.get("pub_date"):
            meta.append(f"📅 {it['pub_date']}")
        if it.get("deadline"):
            meta.append(f"⏰ 截止 {it['deadline']}")
        if it.get("audience"):
            meta.append(f"👥 {it['audience']}")
        if meta:
            print("  ".join(meta))
        print(f"🔗 {it['url']}")
        if it.get("files"):
            print(f"📎 附件: {it['files']}")
        print()
        body = (it.get("body") or "").strip()
        print(body[:3000] + ("…" if len(body) > 3000 else ""))
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------- 附件解析/下载

def extract_attachments(page_url: str) -> list[dict]:
    """抓通知页面，提取附件链接。返回 [{name, url}]。"""
    data, final_url, headers = http_get(page_url)
    html = decode_html(data, headers)
    out, seen = [], set()
    for m in re.finditer(
        r"<a\b[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, re.I | re.S
    ):
        href, text = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        is_file_href = bool(FILE_EXT.search(href.split("#")[0]))
        is_download = re.search(r"download|attach|upload|wbfileid|/file", href, re.I)
        is_file_text = bool(FILE_EXT.search(text))
        if not (is_file_href or is_download or is_file_text):
            continue
        full = urllib.parse.urljoin(final_url, href)
        if full in seen:
            continue
        seen.add(full)
        name = text or urllib.parse.unquote(full.rsplit("/", 1)[-1].split("?")[0])
        out.append({"name": sanitize_filename(name), "url": full})
    return out


def cmd_files(args) -> int:
    try:
        atts = extract_attachments(args.url)
    except Exception as e:
        print(f"抓取页面失败: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(atts, ensure_ascii=False, indent=2))
        return 0
    if not atts:
        print("该页面没有发现附件链接")
        return 0
    print(f"发现 {len(atts)} 个附件：")
    for a in atts:
        print(f"- {a['name']}\n  {a['url']}")
    return 0


def filename_from_response(resp_headers, fallback: str) -> str:
    cd = (resp_headers.get("Content-Disposition") or "") if resp_headers else ""
    m = re.search(r"filename\*?=(?:UTF-8'')?[\"']?([^\"';]+)", cd, re.I)
    if m:
        try:
            name = urllib.parse.unquote(m.group(1))
            if name:
                return sanitize_filename(name)
        except Exception:
            pass
    return sanitize_filename(fallback)


def cmd_fetch(args) -> int:
    try:
        atts = extract_attachments(args.url)
    except Exception as e:
        print(f"抓取页面失败: {e}", file=sys.stderr)
        return 1
    if not atts:
        print("该页面没有发现附件，无法下载")
        return 1

    out_dir = Path(args.out or DEFAULT_OUT)
    # 每条通知一个子目录：优先用页面里的新闻 id，避免 content.jsp 型 URL 撞名
    m = re.search(r"wbnewsid=(\d+)", args.url) or re.search(r"/(\d{3,})\.htm", args.url)
    slug = m.group(1) if m else None
    if not slug:
        slug = sanitize_filename(
            urllib.parse.unquote(args.url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]) or "notice"
        )
    slug = sanitize_filename(f"n{slug}")
    save_dir = out_dir / slug
    save_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for a in atts:
        try:
            data, final_url, headers = http_get(a["url"], timeout=60.0)
        except Exception as e:
            print(f"⚠️ 下载失败: {a['name']} → {e}", file=sys.stderr)
            continue
        name = filename_from_response(headers, a["name"])
        if not Path(name).suffix and FILE_EXT.search(a["url"]):
            m = FILE_EXT.search(a["url"])
            if m:
                name += m.group(1) if m.group(1).startswith(".") else "." + m.group(1)
        path = save_dir / name
        n = 2
        while path.exists():
            path = save_dir / f"{path.stem}({n}){path.suffix}"
            n += 1
        path.write_bytes(data)
        saved.append({"name": name, "path": str(path), "size": len(data), "url": a["url"]})
        print(f"✅ 已下载: {name} ({len(data)} bytes)")

    if args.json:
        print(json.dumps(saved, ensure_ascii=False, indent=2))
    if not saved:
        print("没有成功下载任何附件", file=sys.stderr)
        return 1
    print("\n保存位置（发给用户时用这些绝对路径）：")
    for s in saved:
        print(s["path"])
    return 0


# ---------------------------------------------------------------- refresh

def cmd_refresh(args) -> int:
    base = (args.base or RADAR_BASE).rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/refresh", timeout=10) as r:
            json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"触发抓取失败（school-radar 是否在跑？{base}）: {e}", file=sys.stderr)
        return 1
    print("已触发抓取，等待完成…")
    deadline = time.time() + (args.wait or 360)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/items?days=1", timeout=15) as r:
                d = json.loads(r.read().decode("utf-8"))
            if not d.get("crawling"):
                n = len(d.get("items") or [])
                print(f"抓取完成（近 1 天 {n} 条可见）。要查历史请继续用 search。")
                return 0
        except Exception:
            pass
        time.sleep(5)
    print("等待超时，抓取可能仍在进行，稍后用 search 查看结果")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="school-radar 通知查询/附件下载")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="搜索历史通知")
    p.add_argument("keywords", nargs="+", help="关键词（可多个，AND）")
    p.add_argument("--days", type=int, default=0, help="只看最近 N 天（0=不限）")
    p.add_argument("--category", default="", help="分类 id，如 contest/makeup/money")
    p.add_argument("--limit", type=int, default=20, help="最多返回条数（默认 20）")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", help="通知详情")
    p.add_argument("url", help="通知 URL")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("files", help="列出附件链接")
    p.add_argument("url", help="通知 URL")
    p.set_defaults(func=cmd_files)

    p = sub.add_parser("fetch", help="下载附件")
    p.add_argument("url", help="通知 URL")
    p.add_argument("--out", default="", help=f"保存目录（默认 {DEFAULT_OUT}）")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("refresh", help="触发一次全量抓取")
    p.add_argument("--base", default="", help="school-radar 地址")
    p.add_argument("--wait", type=int, default=360, help="最多等待秒数")
    p.set_defaults(func=cmd_refresh)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
