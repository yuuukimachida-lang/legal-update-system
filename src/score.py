"""
取得済アイテム (output/fetched.json) を Claude で重要度判定し、
output/scored.json として保存する。

設計:
- LLM には axis スコアと判定理由のみ返してもらう (構造化出力)
- final_score = sum(axis_scores) * source.priority_weight は Python 側で計算
- importance (high/mid/low) も Python 側で閾値判定
- ルール (importance_rules.yaml) をシステムプロンプトに載せて prompt caching
  → 同じシステム前文を複数アイテムで使い回す典型的な caching 用途
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import anthropic
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
FETCHED_PATH = ROOT / "output" / "fetched.json"
SOURCES_PATH = ROOT / "sources.yaml"
RULES_PATH = ROOT / "importance_rules.yaml"
OUTPUT_PATH = ROOT / "output" / "scored.json"

MODEL = "claude-opus-4-7"
MAX_TOKENS = 4096


# ---- 構造化出力スキーマ -------------------------------------------------------

class AxisScore(BaseModel):
    """各軸の評価結果。value は importance_rules.yaml の選択肢のいずれか。"""
    value: str = Field(description="importance_rules.yaml の選択肢から選んだ値")
    score: int = Field(description="その選択肢に対応するスコア")


class ScoringOutput(BaseModel):
    legal_form: AxisScore
    enforcement_timing: AxisScore
    obligation_level: AxisScore
    client_scope: AxisScore
    practical_impact: AxisScore
    judgment_reason: str = Field(
        description="判定理由を1〜3文で。UI に表示されるためユーザー (弁護士) 視点で書く。"
    )


# ---- final_score 計算 ---------------------------------------------------------

@dataclass
class AxisBreakdown:
    legal_form: dict
    enforcement_timing: dict
    obligation_level: dict
    client_scope: dict
    practical_impact: dict


@dataclass
class ScoredItem:
    id: str
    source_id: str
    source_name: str
    title: str
    url: str
    published_at: str
    body_text: str
    importance: Literal["high", "mid", "low"]
    final_score: float
    axis_breakdown: dict
    source_weight_applied: float
    judgment_reason: str


def compute_final_score(scoring: ScoringOutput, priority_weight: float) -> float:
    sum_scores = (
        scoring.legal_form.score
        + scoring.enforcement_timing.score
        + scoring.obligation_level.score
        + scoring.client_scope.score
        + scoring.practical_impact.score
    )
    return sum_scores * priority_weight


def classify(final_score: float, thresholds: dict) -> Literal["high", "mid", "low"]:
    if final_score >= thresholds["high"]:
        return "high"
    if final_score >= thresholds["mid"]:
        return "mid"
    return "low"


# ---- システムプロンプト構築 ---------------------------------------------------

def build_system_prompt(rules_yaml_text: str) -> str:
    """importance_rules.yaml の内容をそのままシステムプロンプトに埋め込む。
    YAML 構造は LLM にとっても読みやすく、ルールの正本を二重管理する必要がない。
    """
    return f"""あなたは日本の法令アップデートを評価する専門アシスタントです。
弁護士・法務担当者向けの週次配信のために、各アイテムの重要度判定を行います。

以下のルール (YAML) に従って、与えられた法令アップデートアイテムを評価し、
構造化出力スキーマに沿って各軸の評価値とスコア、判定理由を返してください。

## 評価ルール

```yaml
{rules_yaml_text}
```

## 重要事項

- 各軸の `value` は、ルールの `scores` セクションに記載された選択肢のいずれかを
  そのまま使用してください (新しい選択肢を作らないこと)。
- `score` は、選んだ `value` に対応する数値をルールから正確に転記してください。
- 客観軸 (legal_form, enforcement_timing, obligation_level) は、タイトル・本文の
  メタ情報から事実ベースで抽出してください。
- 主観軸 (client_scope, practical_impact) は、本文の内容と実務インパクトを
  弁護士視点で評価してください。
- `judgment_reason` は、なぜこの重要度判定になったかを1〜3文で簡潔に。
  UI 上で「AI仮判定の理由」として表示されます。
- final_score の計算と high/mid/low の閾値判定は呼び出し側で行うため、
  あなたは axis スコアと judgment_reason のみ返せば OK です。
"""


# ---- Claude 呼び出し ----------------------------------------------------------

def score_item(
    client: anthropic.Anthropic,
    system_prompt: str,
    item: dict,
) -> tuple[ScoringOutput, anthropic.types.Usage]:
    user_content = f"""## アイテム情報

- ソース: {item['source_name']} ({item['source_id']})
- 公表日: {item['published_at']}
- タイトル: {item['title']}
- URL: {item['url']}

## 本文

{item['body_text']}
"""

    response = client.messages.parse(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        # システムプロンプト (固定) を ephemeral cache 対象に。
        # 1件目で書込み (1.25x)、以降の N-1 件は読込み (0.1x) になる。
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
        output_format=ScoringOutput,
    )
    return response.parsed_output, response.usage


# ---- メイン -------------------------------------------------------------------

def main() -> None:
    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("[error] ANTHROPIC_API_KEY が設定されていません。.env を確認してください。")
        sys.exit(1)
    # .env の = の直後のスペース等で 401 にならないよう、念のため strip 後の値を再設定
    os.environ["ANTHROPIC_API_KEY"] = api_key

    fetched = json.loads(FETCHED_PATH.read_text(encoding="utf-8"))
    if not fetched:
        print("[warn] fetched.json が空です。fetch.py を先に実行してください。")
        sys.exit(0)

    sources_data = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8"))
    source_by_id = {s["id"]: s for s in sources_data["sources"]}

    rules_yaml_text = RULES_PATH.read_text(encoding="utf-8")
    rules = yaml.safe_load(rules_yaml_text)
    thresholds = rules["thresholds"]

    system_prompt = build_system_prompt(rules_yaml_text)
    client = anthropic.Anthropic()

    print(f"重要度判定: {len(fetched)}件\n")

    scored: list[ScoredItem] = []
    cache_read_total = 0
    cache_create_total = 0
    input_total = 0
    output_total = 0

    for i, item in enumerate(fetched, 1):
        priority_weight = source_by_id[item["source_id"]].get("priority_weight", 1.0)

        try:
            output, usage = score_item(client, system_prompt, item)
        except anthropic.AuthenticationError as e:
            print(f"  [{i}/{len(fetched)}] [auth error] APIキーが無効です。.envを確認してください。")
            print(f"  詳細: {e}")
            sys.exit(1)
        except anthropic.APIError as e:
            print(f"  [{i}/{len(fetched)}] [error] {e}")
            continue

        cache_create_total += usage.cache_creation_input_tokens or 0
        cache_read_total += usage.cache_read_input_tokens or 0
        input_total += usage.input_tokens
        output_total += usage.output_tokens

        final_score = compute_final_score(output, priority_weight)
        importance = classify(final_score, thresholds)

        scored.append(ScoredItem(
            id=item["id"],
            source_id=item["source_id"],
            source_name=item["source_name"],
            title=item["title"],
            url=item["url"],
            published_at=item["published_at"],
            body_text=item["body_text"],
            importance=importance,
            final_score=round(final_score, 2),
            axis_breakdown={
                "legal_form": output.legal_form.model_dump(),
                "enforcement_timing": output.enforcement_timing.model_dump(),
                "obligation_level": output.obligation_level.model_dump(),
                "client_scope": output.client_scope.model_dump(),
                "practical_impact": output.practical_impact.model_dump(),
            },
            source_weight_applied=priority_weight,
            judgment_reason=output.judgment_reason,
        ))

        # キャッシュヒット率を直近の usage から把握 (デバッグ用)
        # parse() は内部で1回 messages.create を呼ぶので usage は scored[-1] には残らない。
        # 代わりにグローバル累計だけ表示するため、ここでは省略し最後にまとめて出す。

        print(f"  [{i}/{len(fetched)}] {importance:>4} (score={final_score:>5.1f}) - {item['title'][:50]}")

    OUTPUT_PATH.write_text(
        json.dumps([asdict(s) for s in scored], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 内訳サマリ
    by_imp = {"high": 0, "mid": 0, "low": 0}
    for s in scored:
        by_imp[s.importance] += 1
    print(f"\n判定完了: high={by_imp['high']} / mid={by_imp['mid']} / low={by_imp['low']}")
    print(f"→ {OUTPUT_PATH}")

    # トークン使用量サマリ (キャッシュヒット率の確認用)
    total_input = input_total + cache_create_total + cache_read_total
    if total_input > 0:
        cache_hit_rate = cache_read_total / total_input * 100
        print(f"\nトークン使用量:")
        print(f"  入力 (uncached)  : {input_total:>7,}")
        print(f"  キャッシュ書込み  : {cache_create_total:>7,}  (1.25x コスト)")
        print(f"  キャッシュ読込み  : {cache_read_total:>7,}  (0.1x コスト) ← cache hit {cache_hit_rate:.1f}%")
        print(f"  出力             : {output_total:>7,}")
        # Opus 4.7 概算コスト ($5/$25 per 1M tokens, cache write 1.25x, cache read 0.1x)
        cost = (
            input_total * 5.00 / 1_000_000
            + cache_create_total * 6.25 / 1_000_000
            + cache_read_total * 0.50 / 1_000_000
            + output_total * 25.00 / 1_000_000
        )
        print(f"  概算コスト        : ${cost:.4f}")


if __name__ == "__main__":
    main()
