---
name: Project - Legal Update System
description: 週次法令アップデート自動配信システム。日本の会社法/金商法/REIT/労働法/GC領域
type: project
---

**目的**: 弁護士・法務向けに、毎週月曜朝に法令改正・パブコメ・判例等を重要度判定付きで配信するシステム。

**Why**: AIスクールの課題として GitHub Actions を自動化ツールのトリガーに使う構成を学ぶことも目的の一つ。

**技術スタック (2026-05-02 決定)**:
- 言語: Python
- LLM: Claude API (Anthropic)
- 実行環境: GitHub Actions (週次cron)

**進め方**:
- まず1ソース（FSA RSS など）だけでフェーズ1〜3を貫通する最小プロトタイプを作る
- そのあとソース追加・続報検出・二次ソース活用を順次足す
- 閾値キャリブレーションは `importance_rules.yaml` 記載の通り運用4週間後に実施

**How to apply**:
- 新機能を提案するときは「プロトタイプ完成 → 全ソース対応 → 続報検出 → 運用機能」の順序を尊重し、フェーズ飛ばしを避ける
- ファイル `sources.yaml`, `importance_rules.yaml`, `email-mockup.html`/`deploy/index.html` がシステム仕様の正本
