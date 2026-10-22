# -*- coding: utf-8 -*-
"""
fastintel —— 邀请制 AI 产品情报监控
零依赖（只用 Python 标准库），抓取 -> 关键词过滤 -> 去重 -> 推送 -> 生成看板

用法:
    python monitor.py            正常跑一次
    python monitor.py --dry      只看结果，不推送
    python monitor.py --rebuild  不抓取，仅用已有数据重画看板
"""
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
CONFIG_PATH = os.path.join(BASE, "config.json")
SEEN_PATH = os.path.join(DATA, "seen.json")
SCHED_PATH = os.path.join(DATA, "schedule.json")
FEED_PATH = os.path.join(DATA, "feed.json")
TEMPLATE_PATH = os.path.join(BASE, "template.html")
DASH_PATH = os.path.join(BASE, "dashboard.html")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ---------------------------------------------------------------- 基础工具
def now_utc():
    return datetime.now(timezone.utc)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def http_get(url, timeout=12, headers=None):
    h = {
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/xml, application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Encoding": "gzip",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
    return raw


def strip_tags(s):
    if not s:
        return ""
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
          .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", s).strip()


def parse_time(value):
    """尽量把各种时间格式转成 aware datetime(UTC)，失败返回 None"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    v = str(value).strip()
    if not v:
        return None
    if re.fullmatch(r"\d{9,13}", v):
        n = float(v)
        if n > 1e11:
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc)
    try:
        dt = parsedate_to_datetime(v)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(v.replace("Z", "+0000") if fmt.endswith("%z") else v, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- 解析器
def _local(tag):
    return tag.split("}")[-1].lower() if isinstance(tag, str) else ""


def _first_text(el, names):
    for child in el:
        if _local(child.tag) in names:
            if child.text and child.text.strip():
                return child.text.strip()
            for sub in child:
                if _local(sub.tag) == "href" and sub.get("href"):
                    return sub.get("href")
    return ""


def _first_attr(el, names, attr):
    for child in el:
        if _local(child.tag) in names and child.get(attr):
            return child.get(attr)
    return ""


def parse_feed(raw):
    """解析 RSS2.0 / Atom，返回 [{title, link, summary, published}]"""
    out = []
    try:
        text = raw.decode("utf-8", errors="replace").lstrip("\ufeff \r\n\t")
        root = ET.fromstring(text)
    except Exception as e:
        raise ValueError(f"XML 解析失败: {e}")

    for node in root.iter():
        name = _local(node.tag)
        if name not in ("item", "entry"):
            continue
        title = strip_tags(_first_text(node, ("title",)))
        link = (_first_text(node, ("link",)) or _first_attr(node, ("link",), "href")
                or _first_attr(node, ("guid",), "isPermaLink") or "")
        if link and not link.startswith("http"):
            link = _first_attr(node, ("link",), "href") or ""
        summary = strip_tags(_first_text(node, ("description", "summary", "content", "encoded")))
        pub = (_first_text(node, ("pubdate", "published", "updated", "date"))
               or _first_attr(node, ("published", "updated"), "href"))
        out.append({
            "title": title,
            "link": link,
            "summary": summary,
            "published": parse_time(pub),
        })
    return out


def fetch_rss(src):
    return parse_feed(http_get(src["url"], src.get("timeout", 12)))


def fetch_hn(src):
    """Hacker News（Algolia 实时索引，延迟通常 < 2 分钟）"""
    q = urllib.parse.quote(src.get("query", ""))
    url = (f"https://hn.algolia.com/api/v1/search_by_date?query={q}"
           f"&tags=story&hitsPerPage={src.get('limit', 40)}")
    data = json.loads(http_get(url).decode("utf-8", errors="replace"))
    items = []
    for h in data.get("hits", []):
        link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        items.append({
            "title": h.get("title") or "",
            "link": link,
            "summary": h.get("story_text") or "",
            "published": parse_time(h.get("created_at_i")),
        })
    return items


def fetch_reddit(src):
    url = src["url"].rstrip("/") + f"/new.json?limit={src.get('limit', 40)}"
    data = json.loads(http_get(url, headers={"Accept": "application/json"}).decode("utf-8", errors="replace"))
    items = []
    for c in data.get("data", {}).get("children", []):
        d = c.get("data", {})
        items.append({
            "title": d.get("title", ""),
            "link": "https://www.reddit.com" + d.get("permalink", ""),
            "summary": d.get("selftext", "")[:600],
            "published": parse_time(d.get("created_utc")),
        })
    return items


def fetch_telegram(src):
    """t.me/s/<频道> 公开预览页，无需 API、无需登录"""
    chan = src["channel"].lstrip("@")
    html = http_get(f"https://t.me/s/{chan}").decode("utf-8", errors="replace")
    items = []
    blocks = re.split(r'<div class="tgme_widget_message\b', html)[1:]
    for b in blocks:
        m_text = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', b, re.S)
        m_time = re.search(r'<time[^>]*datetime="([^"]+)"', b)
        m_link = re.search(r'data-post="([^"]+)"', b)
        if not (m_text and m_time):
            continue
        items.append({
            "title": strip_tags(m_text.group(1))[:180],
            "link": f"https://t.me/{m_link.group(1)}" if m_link else f"https://t.me/{chan}",
            "summary": strip_tags(m_text.group(1)),
            "published": parse_time(m_time.group(1)),
        })
    return items


def fetch_github(src):
    q = urllib.parse.quote(src.get("query", "ai"))
    url = f"https://api.github.com/search/repositories?q={q}&sort=updated&order=desc&per_page={src.get('limit', 30)}"
    data = json.loads(http_get(url, headers={"Accept": "application/vnd.github+json"}).decode("utf-8", errors="replace"))
    return [{
        "title": it.get("full_name", ""),
        "link": it.get("html_url", ""),
        "summary": it.get("description") or "",
        "published": parse_time(it.get("pushed_at") or it.get("updated_at")),
    } for it in data.get("items", [])]


def fetch_discord(src):
    """Discord 频道消息（最快的一手源：内测群里官方的发布就发生在这里）
    需要一个 Bot Token，并把 Bot 拉进目标服务器，权限勾选「读取消息历史」"""
    cid = src["channel_id"]
    url = f"https://discord.com/api/v10/channels/{cid}/messages?limit={src.get('limit', 30)}"
    raw = http_get(url, headers={"Authorization": f"Bot {src['token']}",
                                 "Accept": "application/json"}, timeout=10)
    data = json.loads(raw.decode("utf-8", errors="replace"))
    if isinstance(data, dict) and data.get("message"):
        raise ValueError(f"Discord: {data['message'][:60]}")
    items = []
    for m in data:
        content = m.get("content") or ""
        if not content:
            continue
        items.append({
            "title": content[:180].replace("\n", " "),
            "link": (f"https://discord.com/channels/{src.get('guild_id', '@me')}/{cid}/{m.get('id')}"),
            "summary": content[:800],
            "published": parse_time(m.get("timestamp")),
        })
    return items



def fetch_openrouter(src):
    """模型雷达：OpenRouter 列出全网可用模型及上架时间。
    新模型常在这里先出现，而且不需要猜关键词——出现即信号。"""
    raw = http_get("https://openrouter.ai/api/v1/models", timeout=25)
    data = json.loads(raw.decode("utf-8", errors="replace"))
    items, seen = [], set()
    for m in data.get("data", []):
        slug = m.get("canonical_slug") or m.get("id") or ""
        if not slug or slug in seen:
            continue
        seen.add(slug)
        name = m.get("name") or slug
        items.append({
            "title": f"新模型上架 · {name}",
            "link": f"https://openrouter.ai/{m.get('id', '')}",
            "summary": (m.get("description") or "")[:400],
            "published": parse_time(m.get("created")),
        })
    return items


FETCHERS = {
    "rss": fetch_rss,
    "hn": fetch_hn,
    "reddit": fetch_reddit,
    "telegram": fetch_telegram,
    "discord": fetch_discord,
    "github": fetch_github,
    "openrouter": fetch_openrouter,
}


# ---------------------------------------------------------------- 过滤
def _kw_pattern(kw):
    """英文关键词用词边界匹配，避免 cue 命中 because、muse 命中 museum"""
    k = kw.lower()
    if re.fullmatch(r"[a-z0-9 .+#\-]+", k):
        return re.compile(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])")
    return re.compile(re.escape(k))


def match_keywords(item, cfg):
    """返回命中的关键词列表"""
    haystack = f"{item['title']} {item['summary']}".lower()
    for kw in cfg["keywords"].get("must_not", []):
        if kw.lower() in haystack:
            return []
    topic = cfg["keywords"].get("topic")
    if topic and not any(_kw_pattern(t).search(haystack) for t in topic):
        return []
    hits = [kw for kw in cfg["keywords"]["any"] if _kw_pattern(kw).search(haystack)]
    return hits


def score_of(hits, cfg):
    priority = {k.lower() for k in cfg["keywords"].get("priority", [])}
    return sum(3 if h.lower() in priority else 1 for h in hits)


def item_key(item):
    raw = (item.get("link") or item.get("title") or "").strip()
    raw = re.sub(r"[?#].*$", "", raw) or raw
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


# ---------------------------------------------------------------- 推送
def push_all(cfg, new_items):
    # 看板里的字段叫 url，实时条目叫 link，统一一下
    for it in new_items:
        if not it.get("link"):
            it["link"] = it.get("url", "")
    sent = []
    for ch in cfg.get("channels", []):
        if not ch.get("enabled"):
            continue
        try:
            if ch["type"] == "bark":
                for it in new_items[:ch.get("max", 5)]:
                    title = it["title"][:60]
                    # 正文放来源和延迟，锁屏一眼能看出"哪来的、多快"
                    bits = [it.get("src") or ""]
                    if it.get("delay") is not None:
                        bits.append(f"{it['delay']} 分钟前")
                    body = " · ".join(b for b in bits if b) or "fastintel"
                    url = (f"https://api.day.app/{ch['key']}/{urllib.parse.quote(title)}"
                           f"/{urllib.parse.quote(body)}?url={urllib.parse.quote(it['link'])}"
                           f"&group=fastintel&level=timeSensitive")
                    resp = http_get(url, timeout=8)
                    # 校验服务器真的收下了，否则日志里全是假成功
                    try:
                        rj = json.loads(resp.decode("utf-8", "ignore"))
                        if rj.get("code") != 200:
                            log(f"  bark 拒收: {rj}")
                    except Exception:
                        pass
            elif ch["type"] == "feishu":
                body = "\n".join(f"• {i['title']}\n  {i['link']}" for i in new_items[:8])
                _post_json(ch["webhook"], {"msg_type": "text", "content": {"text": f"情报 {len(new_items)} 条\n{body}"}})
            elif ch["type"] == "wecom":
                body = "\n".join(f"• {i['title']}\n  {i['link']}" for i in new_items[:8])
                _post_json(ch["webhook"], {"msgtype": "text", "text": {"content": f"情报 {len(new_items)} 条\n{body}"}})
            elif ch["type"] == "dingtalk":
                body = "\n".join(f"- {i['title']} {i['link']}" for i in new_items[:8])
                _post_json(ch["webhook"], {"msgtype": "text", "text": {"content": f"情报 {len(new_items)} 条\n{body}"}})
            elif ch["type"] == "telegram":
                body = "\n".join(f"• {i['title']}\n{i['link']}" for i in new_items[:8])
                _post_json(f"https://api.telegram.org/bot{ch['token']}/sendMessage",
                           {"chat_id": ch["chat_id"], "text": f"情报 {len(new_items)} 条\n{body}",
                            "disable_web_page_preview": True})
            elif ch["type"] == "serverchan":
                _post_form(f"https://sctapi.ftqq.com/{ch['key']}.send",
                           {"title": f"情报 {len(new_items)} 条", "desp": "\n\n".join(i["link"] for i in new_items[:8])})
            elif ch["type"] == "console":
                for it in new_items[:ch.get("max", 5)]:
                    print(f"     >> {it['title']}\n        {it['url']}")
            elif ch["type"] == "webhook":
                _post_json(ch["url"], {"count": len(new_items), "items": new_items[:20]})
            sent.append(ch["type"])
        except Exception as e:
            log(f"  推送失败 {ch['type']}: {e}")
    return sent


def _post_json(url, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def _post_form(url, payload):
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


# ---------------------------------------------------------------- 看板
def render_dashboard(cfg, items, stats, recent=None):
    payload = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "stats": stats, "items": items, "recent": recent or []}
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        tpl = f.read()
    html = tpl.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))
    with open(DASH_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"看板已更新 -> {DASH_PATH}")



def check_sources(cfg):
    """体检：逐个测试源的连通性、返回条数、最新一条的时间"""
    print(f"{'源':<26}{'状态':<8}{'条数':>4}  {'耗时':>6}  最新一条")
    print("-" * 78)
    ok = 0
    for src in cfg["sources"]:
        label = (src.get("name") or src.get("url") or src["type"])[:24]
        if not src.get("enabled", True):
            print(f"{label:<26}{'未启用':<8}")
            continue
        t0 = time.time()
        try:
            items = FETCHERS[src["type"]](src)
            cost = time.time() - t0
            newest = max((i["published"] for i in items if i.get("published")), default=None)
            age = ""
            if newest:
                mins = int((now_utc() - newest).total_seconds() // 60)
                age = f"{mins} 分钟前" if mins < 1440 else f"{mins // 1440} 天前"
            print(f"{label:<26}{'通':<8}{len(items):>4}  {cost:>5.1f}s  {age}")
            ok += 1
        except Exception as e:
            print(f"{label:<26}{'不通':<8}{'':>4}  {time.time()-t0:>5.1f}s  {str(e)[:40]}")
    total = sum(1 for s in cfg["sources"] if s.get("enabled", True))
    print("-" * 78)
    print(f"可用 {ok}/{total}")
    if ok < total:
        print("不通的源多半是被网络挡住了。挂上代理后重跑一次本命令即可。")


# ---------------------------------------------------------------- 主流程
def main():
    args = set(sys.argv[1:])
    os.makedirs(DATA, exist_ok=True)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 云端运行时 bark key 由 GitHub Secret 注入，绝不写进仓库
    env_key = os.environ.get("BARK_KEY", "").strip()
    if env_key:
        for ch in cfg.get("channels", []):
            if ch.get("type") == "bark":
                ch["key"] = env_key
                ch["enabled"] = True

    if "--rebuild" in args:
        with open(FEED_PATH, "r", encoding="utf-8") as f:
            old = json.load(f)
        render_dashboard(cfg, old["items"], old["stats"], old.get("recent"))
        return

    if "--check" in args:
        check_sources(cfg)
        return

    if "--test-push" in args:
        with open(FEED_PATH, "r", encoding="utf-8") as f:
            demo = json.load(f)["items"][:3]
        if not demo:
            print("看板里没有内容，先跑一次 monitor.py")
            return
        print(f"用最近 {len(demo)} 条真实情报测试推送...")
        sent = push_all(cfg, demo)
        print(f"已推送: {', '.join(sent) if sent else '没有启用的通道'}")
        return

    dry = "--dry" in args
    first_run = not os.path.exists(SEEN_PATH)
    seen = {}
    if not first_run:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            seen = json.load(f)

    old_items, old_stats = [], {}
    if os.path.exists(FEED_PATH):
        try:
            with open(FEED_PATH, "r", encoding="utf-8") as f:
                prev = json.load(f)
            old_items, old_stats = prev.get("items", []), prev.get("stats", {})
        except Exception:
            pass

    enabled_srcs = [s for s in cfg["sources"] if s.get("enabled", True)]
    log(f"开始扫描 {len(enabled_srcs)} 个源"
        + ("（首次运行：只推送最近 2 小时的消息）" if first_run else ""))

    # 分层轮询：每 tick 跑一次循环，但每个源按自己的 interval_sec 决定要不要抓
    sched = {}
    if os.path.exists(SCHED_PATH):
        try:
            with open(SCHED_PATH, "r", encoding="utf-8") as f:
                sched = json.load(f)
        except Exception:
            sched = {}
    force = "--force" in args
    now_ts = time.time()
    default_iv = cfg.get("default_interval_sec", 600)

    collected, ok_src, bad_src, skipped = [], 0, [], 0
    for src in cfg["sources"]:
        if not src.get("enabled", True):
            continue
        label = src.get("name") or src["url"]
        iv = src.get("interval_sec") or default_iv
        if not force and (now_ts - sched.get(label, 0)) < iv:
            skipped += 1
            continue
        sched[label] = now_ts
        t0 = time.time()
        try:
            items = FETCHERS[src["type"]](src)
            for it in items:
                it["source"] = label
                it["source_tier"] = src.get("tier", 3)
                it["mode"] = src.get("mode", "keyword")
                it["max_age_days"] = src.get("max_age_days")
            collected.extend(items)
            ok_src += 1
            log(f"  [ok]   {label:22s} {len(items):3d} 条  {time.time()-t0:.1f}s")
        except Exception as e:
            bad_src.append(label)
            msg = str(e)[:70]
            log(f"  [fail] {label:22s} {msg}")

    if not collected:
        with open(SCHED_PATH, "w", encoding="utf-8") as f:
            json.dump(sched, f, ensure_ascii=False)
        log(f"本轮没有源到期（跳过 {skipped} 个），等下一个 tick")
        return

    cutoff = now_utc() - timedelta(minutes=cfg.get("first_run_window_minutes", 120))
    fresh, seen_new = [], dict(seen)
    others, other_keys = [], set()
    max_age = timedelta(days=cfg.get("max_age_days", 3))
    for it in collected:
        _pub = it.get("published")
        # 时间闸门：官方源的 RSS 往往一次返回上千条历史，超过窗口的直接丢掉
        _age = timedelta(days=it.get("max_age_days") or cfg.get("max_age_days", 3))
        if _pub and (now_utc() - _pub) > _age:
            continue
        if it.get("mode") == "all":
            hits = ["一手"]
        else:
            hits = match_keywords(it, cfg)
        if not hits:
            # 未命中：只把一级/二级源的新内容放进背景流，证明管道在跑
            k = item_key(it)
            if it["source_tier"] <= 2 and k not in other_keys:
                other_keys.add(k)
                pub = it.get("published")
                others.append({
                    "t": pub.astimezone().strftime("%m-%d %H:%M") if pub else "--",
                    "ts": pub.timestamp() if pub else 0,
                    "src": it["source"],
                    "title": it["title"][:160],
                    "url": it["link"],
                })
            continue
        key = item_key(it)
        if key in seen_new:
            continue
        seen_new[key] = now_utc().isoformat()
        pub = it.get("published")
        delay = None
        if pub:
            delay = max(0, int((now_utc() - pub).total_seconds() // 60))
        # 首次运行：历史条目照常上看板，但不推送，避免开机刷屏
        pushable = not (first_run and pub and pub < cutoff)
        fresh.append({
            "t": pub.astimezone().strftime("%m-%d %H:%M") if pub else "--",
            "ts": pub.timestamp() if pub else 0,
            "src": it["source"],
            "tier": it["source_tier"],
            "title": it["title"][:200],
            "url": it["link"],
            "kw": hits[:6],
            "score": score_of(hits, cfg),
            "delay": delay,
            "found": datetime.now().strftime("%H:%M:%S"),
            "push": pushable and it["source_tier"] <= cfg.get("push_tier_max", 2),
        })

    fresh.sort(key=lambda x: (x["tier"], -x["score"], -x["ts"]))
    merged = (fresh + old_items)[:cfg.get("keep_items", 300)]
    others.sort(key=lambda x: -x["ts"])
    # 按源轮询取样，避免某个高频源（如 GitHub）把背景流刷屏
    buckets, order = {}, []
    for o in others:
        if o["src"] not in buckets:
            buckets[o["src"]] = []
            order.append(o["src"])
        if len(buckets[o["src"]]) < cfg.get("recent_per_source", 8):
            buckets[o["src"]].append(o)
    recent, idx = [], 0
    while len(recent) < cfg.get("recent_items", 40):
        added = False
        for s in order:
            if idx < len(buckets[s]):
                recent.append(buckets[s][idx])
                added = True
                if len(recent) >= cfg.get("recent_items", 40):
                    break
        if not added:
            break
        idx += 1
    seen_new = dict(sorted(seen_new.items(), key=lambda kv: kv[1], reverse=True)[:5000])

    with open(SCHED_PATH, "w", encoding="utf-8") as f:
        json.dump(sched, f, ensure_ascii=False)
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen_new, f, ensure_ascii=False)
    with open(FEED_PATH, "w", encoding="utf-8") as f:
        json.dump({"items": merged, "recent": recent, "stats": {
            "sources_ok": ok_src, "sources_fail": len(bad_src), "failed": bad_src,
            "last_run": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "new_this_run": len(fresh), "total": len(merged), "scanned": len(collected),
        }}, f, ensure_ascii=False, indent=1)

    # 只有一手/准一手源的命中才推送；转载层只上看板，不打扰
    tier_max = cfg.get("push_tier_max", 2)
    to_push = [i for i in fresh if i.get("push") and i["tier"] <= tier_max]
    log(f"本轮命中 {len(fresh)} 条（抓取 {ok_src} 个源 {len(collected)} 条，跳过 {skipped} 个未到期源）")
    for it in fresh[:10]:
        d = f"{it['delay']} 分钟前" if it["delay"] is not None else "时间未知"
        log(f"  * {it['title'][:52]}  [{it['src']} · {d}]")

    if to_push and not dry:
        sent = push_all(cfg, to_push)
        if sent:
            log(f"已推送: {', '.join(sent)}")

    render_dashboard(cfg, merged, {
        "sources_ok": ok_src, "sources_fail": len(bad_src), "failed": bad_src,
        "last_run": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "new_this_run": len(fresh), "total": len(merged), "scanned": len(collected),
    }, recent)


if __name__ == "__main__":
    main()
