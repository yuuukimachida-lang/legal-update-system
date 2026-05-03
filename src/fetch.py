"""
sources.yaml で active かつ fetch_method=rss のソースから、過去7日分のアイテムを取得する。

プロトタイプ段階では FSA のみを対象にしている (sources.yaml の他のソースは active=true でも
fetch_method が scrape/api/manual のため、このスクリプトでは無視される)。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

# Windows コンソールでの日本語文字化け対策。標準出力を UTF-8 にする。
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = ROOT / "sources.yaml"
OUTPUT_PATH = ROOT / "output" / "fetched.json"

LOOKBACK_DAYS = 7
HTTP_TIMEOUT = 30
USER_AGENT = "LegalUpdateBot/0.1 (+https://github.com/) prototype"

# プロトタイプ段階で取得対象にする source_id の許可リスト。
# None にすると active かつ fetch_method=rss の全ソースを対象にする。
# 全ソース対応はプロトタイプ後の課題 (sources.yaml の他 RSS の URL 検証含む)。
PROTOTYPE_SOURCE_FILTER: set[str] | None = {"fsa_news"}

# RFC822 の日付に含まれる "JST" を dateutil が解釈できないため明示的に教える。
JST = timezone(timedelta(hours=9))
TZINFOS = {"JST": JST}

# RSS タイトル先頭のカテゴリ接頭辞のうち、法令アップデートに無関係なもの。
# ここに載っているカテゴリのアイテムは取得段階で除外する。
EXCLUDED_TITLE_PREFIXES = (
    "採用",
    "記者会見",
    "調達",
    "イベント",
    "シンポジウム",
)


@dataclass
class FetchedItem:
    id: str                  # ソース横断で安定な識別子 (URL の SHA1)
    source_id: str           # sources.yaml の id
    source_name: str
    title: str
    url: str
    published_at: str        # ISO8601
    body_text: str           # 本文 HTML から抽出したプレーンテキスト
    fetched_at: str          # ISO8601


def load_sources() -> list[dict]:
    """sources.yaml を読み込み、active かつ fetch_method=rss、かつ
    PROTOTYPE_SOURCE_FILTER に合致するソースのみ返す。"""
    with SOURCES_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    sources = [
        s for s in data["sources"]
        if s.get("active") and s.get("fetch_method") == "rss"
    ]
    if PROTOTYPE_SOURCE_FILTER is not None:
        sources = [s for s in sources if s["id"] in PROTOTYPE_SOURCE_FILTER]
    return sources


def is_excluded(title: str) -> bool:
    """採用情報など法令アップデート対象外のアイテムを除外。"""
    for prefix in EXCLUDED_TITLE_PREFIXES:
        if title.startswith(prefix + ",") or title.startswith(prefix + "・"):
            return True
    return False


def extract_body_text(url: str) -> str:
    """記事 URL から本文テキストを抽出。失敗時は空文字。"""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [warn] 本文取得失敗: {url} ({e})")
        return ""

    resp.encoding = resp.apparent_encoding or "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    # スクリプト・スタイル・ナビ系は除去
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    # 金融庁ページは <div id="contents"> に本文がある (なければ body 全体)
    main = soup.find(id="contents") or soup.find("main") or soup.body
    text = main.get_text(separator="\n", strip=True) if main else ""

    # 連続改行・空白を圧縮
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    # FSA ページは「サイトマップ」以降に巨大なナビメニューが続くので切り捨てる。
    # 他省庁のページに対応する際は同様の boilerplate cutoff を追加する。
    for cutoff_marker in ("サイトマップ", "ページの先頭に戻る"):
        idx = text.find(cutoff_marker)
        if idx > 0:
            text = text[:idx].rstrip()
            break

    return text[:8000]  # LLM コスト抑制のため8000字に切る


def fetch_source(source: dict, since: datetime) -> list[FetchedItem]:
    """単一ソースから since 以降のアイテムを取得。"""
    feed_url = source["feed_url"]
    print(f"[{source['id']}] {feed_url}")

    parsed = feedparser.parse(feed_url)
    if parsed.bozo and not parsed.entries:
        print(f"  [error] フィード取得失敗: {parsed.bozo_exception}")
        return []

    items: list[FetchedItem] = []
    for entry in parsed.entries:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        published_raw = entry.get("published") or entry.get("updated") or ""

        if not title or not link:
            continue
        if is_excluded(title):
            continue

        try:
            published = date_parser.parse(published_raw, tzinfos=TZINFOS)
        except (ValueError, TypeError):
            print(f"  [warn] 日付パース失敗、スキップ: {title[:40]}")
            continue

        # since 以降のもののみ
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if published < since:
            continue

        body = extract_body_text(link)

        item_id = hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]
        items.append(FetchedItem(
            id=item_id,
            source_id=source["id"],
            source_name=source["name"],
            title=title,
            url=link,
            published_at=published.isoformat(),
            body_text=body,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        ))
        print(f"  + {title[:60]}")

    return items


def main() -> None:
    sources = load_sources()
    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    print(f"取得対象: {len(sources)}ソース / 期間: {since.isoformat()} 以降\n")

    all_items: list[FetchedItem] = []
    for source in sources:
        all_items.extend(fetch_source(source, since))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump([asdict(item) for item in all_items], f, ensure_ascii=False, indent=2)

    print(f"\n取得完了: {len(all_items)}件 → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
