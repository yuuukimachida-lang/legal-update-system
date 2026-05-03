"""
重要度判定済アイテム (output/scored.json) のうち high / mid のものに対して
Claude で要約・実務影響を生成し、output/summarized.json として保存する。

low は要約スキップ (UI でも参考バッジでタイトル表示のみ)。

出力フィールド (UI モックアップと対応):
- headline           : サマリーリストに出す短いタイトル (40〜60字)
- change_summary     : 「変更点」段落 (150〜300字)
- affected_clients   : 「注意が必要なクライアント」タグ (1〜4個)
- practical_action   : 「実務上の対応」段落 (100〜250字)
- key_date           : { label, date_str } 例: { label: "意見締切", date_str: "2026年5月12日" }
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import anthropic
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
SCORED_PATH = ROOT / "output" / "scored.json"
SOURCES_PATH = ROOT / "sources.yaml"
OUTPUT_PATH = ROOT / "output" / "summarized.json"

MODEL = "claude-opus-4-7"
MAX_TOKENS = 4096


# ---- 構造化出力スキーマ -------------------------------------------------------

class KeyDate(BaseModel):
    label: str = Field(description="日付の意味ラベル。例: '意見締切', '施行予定', '公表', '判決'")
    date_str: str = Field(description="日付文字列。例: '2026年5月12日'。不明な場合は空文字。")


class SummaryOutput(BaseModel):
    headline: str = Field(
        description="サマリーリストに表示する短い見出し (40〜60字目安)。元タイトルが冗長な場合は核心を抽出して短くする。"
    )
    change_summary: str = Field(
        description="『変更点』段落。何がどう変わるかを150〜300字で。条文番号・パブコメ番号など具体的引用を含めると良い。"
    )
    affected_clients: list[str] = Field(
        description="影響を受ける顧問先タイプを1〜4個。例: '上場企業（全般）', 'PEファンド', '金融機関', '事業会社（中小）'。"
    )
    practical_action: str = Field(
        description="『実務上の対応』段落。顧問先に対して何を提案・アドバイスすべきかを100〜250字で。"
    )
    key_date: KeyDate = Field(
        description="最も重要な日付 (意見締切 > 施行予定 > 公表日 の優先順位で1つ選ぶ)。"
    )


# ---- データ構造 ---------------------------------------------------------------

@dataclass
class SummarizedItem:
    # scored.json から引き継ぐ
    id: str
    source_id: str
    source_name: str
    title: str
    url: str
    published_at: str
    importance: str
    final_score: float
    judgment_reason: str
    axis_breakdown: dict
    # ソースから派生
    category: str           # 会社法 / 金融商品取引法 / REIT関連 / 労働法 / ジェネラルコーポレート
    regulator: str          # 所管 (例: 金融庁)
    # LLM 生成
    headline: str
    change_summary: str
    affected_clients: list[str]
    practical_action: str
    key_date: dict


# ---- システムプロンプト構築 ---------------------------------------------------

SYSTEM_PROMPT = """あなたは日本の法令アップデートを弁護士・法務担当者向けに要約する専門アシスタントです。
週次配信メールのために、各アイテムの「変更点」「注意が必要なクライアント」「実務上の対応」を
構造化スキーマに沿って生成してください。

## 重要な作成方針

1. **読者は弁護士**。専門用語は適切に使い、噛み砕きすぎない。条文番号・パブコメ番号・
   告示番号などの具体的引用は積極的に残す。
2. **headline は短く核心を**。元タイトルが冗長なら核心を抽出。例:
   - 元: 「『金融商品取引業者等向けの総合的な監督指針』等の一部改正について公表しました。」
   - headline: 「金商業者監督指針 一部改正（令和7年政令247号施行に伴う整備）」
3. **change_summary は事実ベース**。本文に書いてあることだけ。推測や一般論で水増ししない。
4. **affected_clients は具体的に**。「全社」のような曖昧な表現を避け、
   「上場企業（プライム）」「PEファンド」「暗号資産交換業者」のように特定する。
5. **practical_action は実務的に**。「内容を確認すべき」のような自明な助言は避け、
   「定時株主総会前に報酬決定プロセスのレビュー」のような具体的なアクションを書く。
6. **key_date は1つだけ選ぶ**。優先順位: 意見締切 > 施行予定 > 公表日。
   本文から日付が読み取れない場合は date_str を空文字に。
"""


# ---- ソースから派生する情報 ---------------------------------------------------

def derive_category(source: dict) -> str:
    """sources.yaml の category (配列) から代表カテゴリを1つ選ぶ。
    複数ある場合は最初を採用 (FSA は [金融商品取引法, REIT関連, 会社法] → 金融商品取引法)。
    将来的にはアイテム本文から LLM に判定させるべきだがプロトタイプでは簡略化。
    """
    categories = source.get("category", [])
    if isinstance(categories, list) and categories:
        return categories[0]
    if isinstance(categories, str):
        return categories
    return "ジェネラルコーポレート"


def derive_regulator(source: dict) -> str:
    """ソース名から所管官庁を抽出。'金融庁 報道発表' → '金融庁'。"""
    name = source.get("name", "")
    # スペース or 助詞で区切られた最初のトークンを所管とする簡易ルール
    for sep in [" ", "　"]:
        if sep in name:
            return name.split(sep)[0]
    return name


# ---- Claude 呼び出し ----------------------------------------------------------

def summarize_item(
    client: anthropic.Anthropic,
    item: dict,
) -> tuple[SummaryOutput, anthropic.types.Usage]:
    user_content = f"""## アイテム情報

- ソース: {item['source_name']} ({item['source_id']})
- 公表日: {item['published_at']}
- タイトル: {item['title']}
- URL: {item['url']}
- 重要度判定: {item['importance']} (スコア {item['final_score']})
- 判定理由: {item['judgment_reason']}

## 軸スコア (参考)

- 法形式      : {item['axis_breakdown']['legal_form']['value']}
- 施行時期    : {item['axis_breakdown']['enforcement_timing']['value']}
- 拘束力      : {item['axis_breakdown']['obligation_level']['value']}
- 影響範囲    : {item['axis_breakdown']['client_scope']['value']}
- 実務影響度  : {item['axis_breakdown']['practical_impact']['value']}

## 本文

{item['body_text']}
"""

    response = client.messages.parse(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
        output_format=SummaryOutput,
    )
    return response.parsed_output, response.usage


# ---- メイン -------------------------------------------------------------------

def main() -> None:
    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("[error] ANTHROPIC_API_KEY が設定されていません。")
        sys.exit(1)
    os.environ["ANTHROPIC_API_KEY"] = api_key

    scored = json.loads(SCORED_PATH.read_text(encoding="utf-8"))
    sources_data = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8"))
    source_by_id = {s["id"]: s for s in sources_data["sources"]}

    # 要約対象は high / mid のみ
    targets = [x for x in scored if x["importance"] in ("high", "mid")]
    print(f"要約対象: {len(targets)}件 (全{len(scored)}件中, high+mid のみ)\n")

    client = anthropic.Anthropic()

    summarized: list[SummarizedItem] = []
    cache_create_total = 0
    cache_read_total = 0
    input_total = 0
    output_total = 0

    for i, item in enumerate(targets, 1):
        try:
            output, usage = summarize_item(client, item)
        except anthropic.APIError as e:
            print(f"  [{i}/{len(targets)}] [error] {e}")
            continue

        cache_create_total += usage.cache_creation_input_tokens or 0
        cache_read_total += usage.cache_read_input_tokens or 0
        input_total += usage.input_tokens
        output_total += usage.output_tokens

        source = source_by_id[item["source_id"]]
        summarized.append(SummarizedItem(
            id=item["id"],
            source_id=item["source_id"],
            source_name=item["source_name"],
            title=item["title"],
            url=item["url"],
            published_at=item["published_at"],
            importance=item["importance"],
            final_score=item["final_score"],
            judgment_reason=item["judgment_reason"],
            axis_breakdown=item["axis_breakdown"],
            category=derive_category(source),
            regulator=derive_regulator(source),
            headline=output.headline,
            change_summary=output.change_summary,
            affected_clients=output.affected_clients,
            practical_action=output.practical_action,
            key_date=output.key_date.model_dump(),
        ))

        print(f"  [{i}/{len(targets)}] {item['importance']:>4} - {output.headline[:50]}")

    # low の元アイテムも (要約なしで) summarized.json に含める。
    # render.py がここから1つのソースで全件レンダリングできるようにするため。
    for item in scored:
        if item["importance"] == "low":
            source = source_by_id[item["source_id"]]
            summarized.append(SummarizedItem(
                id=item["id"],
                source_id=item["source_id"],
                source_name=item["source_name"],
                title=item["title"],
                url=item["url"],
                published_at=item["published_at"],
                importance=item["importance"],
                final_score=item["final_score"],
                judgment_reason=item["judgment_reason"],
                axis_breakdown=item["axis_breakdown"],
                category=derive_category(source),
                regulator=derive_regulator(source),
                headline=item["title"],   # low は元タイトルそのまま
                change_summary="",
                affected_clients=[],
                practical_action="",
                key_date={"label": "公表", "date_str": item["published_at"][:10]},
            ))

    OUTPUT_PATH.write_text(
        json.dumps([asdict(s) for s in summarized], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n要約完了 → {OUTPUT_PATH}")

    # コスト集計
    total_input = input_total + cache_create_total + cache_read_total
    if total_input > 0:
        cache_hit_rate = cache_read_total / total_input * 100 if total_input else 0
        cost = (
            input_total * 5.00 / 1_000_000
            + cache_create_total * 6.25 / 1_000_000
            + cache_read_total * 0.50 / 1_000_000
            + output_total * 25.00 / 1_000_000
        )
        print(f"\nトークン使用量:")
        print(f"  入力 (uncached)  : {input_total:>7,}")
        print(f"  キャッシュ書込み  : {cache_create_total:>7,}")
        print(f"  キャッシュ読込み  : {cache_read_total:>7,}  ← cache hit {cache_hit_rate:.1f}%")
        print(f"  出力             : {output_total:>7,}")
        print(f"  概算コスト        : ${cost:.4f}")


if __name__ == "__main__":
    main()
