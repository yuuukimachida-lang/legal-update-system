"""
output/summarized.json と templates/digest.html.j2 から
output/index.html を生成する。

カテゴリ並び順は importance_rules.yaml / sources.yaml と整合させる:
  会社法 → 金融商品取引法 → REIT関連 → 労働法 → ジェネラルコーポレート

詳細レポートは high → mid の順で並べ、同重要度内では final_score 降順。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
SUMMARIZED_PATH = ROOT / "output" / "summarized.json"
TEMPLATES_DIR = ROOT / "templates"
OUTPUT_HTML = ROOT / "output" / "index.html"

JST = timezone(timedelta(hours=9))

CATEGORY_ORDER = [
    "会社法",
    "金融商品取引法",
    "REIT関連",
    "労働法",
    "ジェネラルコーポレート",
]

IMPORTANCE_ORDER = {"high": 0, "mid": 1, "low": 2}


def group_by_category(items: list[dict]) -> list[dict]:
    """カテゴリ別にグループ化し、CATEGORY_ORDER の順に並べる。
    各カテゴリ内では importance (high→mid→low) → final_score 降順。
    """
    by_cat: dict[str, list[dict]] = {}
    for item in items:
        by_cat.setdefault(item["category"], []).append(item)

    result = []
    seen_cats = set()
    # 既知のカテゴリ順
    for cat in CATEGORY_ORDER:
        if cat in by_cat:
            result.append({"name": cat, "items": _sort_within_category(by_cat[cat])})
            seen_cats.add(cat)
    # 未知のカテゴリは末尾にアルファベット順
    for cat in sorted(set(by_cat.keys()) - seen_cats):
        result.append({"name": cat, "items": _sort_within_category(by_cat[cat])})
    return result


def _sort_within_category(items: list[dict]) -> list[dict]:
    return sorted(
        items,
        key=lambda x: (IMPORTANCE_ORDER[x["importance"]], -x["final_score"]),
    )


def main() -> None:
    items = json.loads(SUMMARIZED_PATH.read_text(encoding="utf-8"))
    if not items:
        print("[warn] summarized.json が空です。")
        sys.exit(0)

    counts = {"high": 0, "mid": 0, "low": 0}
    for x in items:
        counts[x["importance"]] += 1

    # 詳細レポート用 (high+mid のみ、importance/score 降順)
    detail_items = sorted(
        [x for x in items if x["importance"] in ("high", "mid")],
        key=lambda x: (IMPORTANCE_ORDER[x["importance"]], -x["final_score"]),
    )

    # 配信日 = 今日 (週次cron想定)、対象期間 = 過去7日
    now = datetime.now(JST)
    week_start = now - timedelta(days=7)
    next_send = now + timedelta(days=7)

    context = {
        "recipient_email": "your.name@firm.com",  # プロトタイプは固定。本運用時は config 化
        "send_datetime": now.strftime("%Y年%-m月%-d日（月）　%H:%M") if sys.platform != "win32"
                         else now.strftime("%Y年") + f"{now.month}月{now.day}日（月）　" + now.strftime("%H:%M"),
        "week_range": f"{week_start.month}月{week_start.day}日〜{now.month}月{now.day}日",
        "count_high": counts["high"],
        "count_mid": counts["mid"],
        "count_low": counts["low"],
        "categories": group_by_category(items),
        "detail_items": detail_items,
        "next_send_date": f"{next_send.year}年{next_send.month}月{next_send.day}日（月）07:00",
    }

    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=False,
        lstrip_blocks=False,
    )
    template = env.get_template("digest.html.j2")
    html = template.render(**context)

    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_HTML.write_text(html, encoding="utf-8")

    print(f"レンダリング完了: {len(items)}件 → {OUTPUT_HTML}")
    print(f"  重要={counts['high']} / 中={counts['mid']} / 参考={counts['low']}")


if __name__ == "__main__":
    main()
