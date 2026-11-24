"""최신 보안 동향: 공개 피드에서 제목·시각·링크만 가져와 저장한다. 본문은 가져오지 않는다.

고객사 관련 여부는 고객사별로 등록한 제품·제조사 낱말이 제목에 있는지로 코드가 판단한다.
"""
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from defusedxml import ElementTree

from .config import Config
from .db import TS, now, set_state

UA = "Mozilla/5.0 (compatible; feed-reader)"
MAX_BYTES = 8 * 1024 * 1024
_DATE_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y.%m.%d"]


def _download(url: str, timeout: float = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(MAX_BYTES)


def parse_date(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed.strftime(TS)
    except (TypeError, ValueError):
        pass
    cleaned = re.sub(r"(\.\d+)?(Z|[+-]\d{2}:?\d{2})$", "", value)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).strftime(TS)
        except ValueError:
            continue
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(raw: bytes) -> list[dict]:
    """RSS 2.0과 Atom을 읽어 제목·링크·시각만 뽑는다."""
    root = ElementTree.fromstring(raw)
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title = link = published = None
        for child in node:
            name = _local(child.tag)
            if name == "title":
                title = (child.text or "").strip()
            elif name == "link":
                link = (child.text or "").strip() or child.attrib.get("href", "").strip()
            elif name in ("pubdate", "published", "updated", "date") and not published:
                published = parse_date(child.text)
        if title and link and re.match(r"^https?://", link, re.I):
            items.append({"title": re.sub(r"\s+", " ", title)[:300], "link": link[:500],
                          "published_at": published})
    return items


def parse_kev(raw: bytes, recent_days: int = 45) -> list[dict]:
    """악용이 확인된 취약점 목록에서 최근에 추가된 항목만 고른다."""
    data = json.loads(raw.decode("utf-8"))
    cutoff = (datetime.now() - timedelta(days=recent_days)).strftime("%Y-%m-%d")
    items = []
    for v in data.get("vulnerabilities", []):
        added = v.get("dateAdded", "")
        cve = v.get("cveID", "")
        if added < cutoff or not re.match(r"^CVE-\d{4}-\d+$", cve):
            continue
        title = f"{cve} {v.get('vendorProject', '')} {v.get('product', '')} — {v.get('vulnerabilityName', '')}"
        items.append({"title": re.sub(r"\s+", " ", title)[:300],
                      "link": f"https://nvd.nist.gov/vuln/detail/{cve}",
                      "published_at": added + " 00:00:00"})
    return items


def match_customers(title: str, cfg: Config) -> list[str]:
    low = title.lower()
    out = []
    for code, c in cfg.customers.items():
        for word in c.get("products", []):
            w = str(word).lower()
            hit = re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low) if w.isascii() else (w in low)
            if hit:
                out.append(code)
                break
    return out


def fetch_all(conn, cfg: Config) -> dict:
    result = {"added": 0, "errors": []}
    for src in cfg.news.get("sources", []):
        try:
            raw = _download(src["url"])
            if src.get("type") == "kev":
                items = parse_kev(raw, int(src.get("recent_days", 45)))
            else:
                items = parse_feed(raw)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, ElementTree.ParseError) as exc:
            result["errors"].append(f"{src['name']}: {str(exc)[:120]}")
            continue
        for it in items:
            matched = ",".join(match_customers(it["title"], cfg))
            cur = conn.execute(
                "INSERT OR IGNORE INTO news (source, title, link, published_at, fetched_at, matched) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (src["name"], it["title"], it["link"], it["published_at"], now(), matched))
            result["added"] += cur.rowcount
    keep = int(cfg.news.get("keep_days", 30))
    cutoff = (datetime.now() - timedelta(days=keep)).strftime(TS)
    conn.execute("DELETE FROM news WHERE COALESCE(published_at, fetched_at) < ? AND source != 'CISA KEV'",
                 (cutoff,))
    if len(result["errors"]) < len(cfg.news.get("sources", [])):
        set_state(conn, "news_last_ok", now())
    set_state(conn, "news_error", "; ".join(result["errors"]))
    return result
