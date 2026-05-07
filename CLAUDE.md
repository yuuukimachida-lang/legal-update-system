# Legal Update System

日本の法令アップデートを毎週月曜朝に Outlook に自動配信するシステム。

## アーキテクチャ

```
┌─ 月曜 07:00 JST ─┐  ┌─ 月曜 07-08時 JST ─┐
│ GitHub Actions   │  │ GAS time trigger   │
│ (cron)           │  │                    │
│  ↓               │  │  ↓                 │
│ python main.py   │  │ UrlFetchApp で     │
│  ├─ fetch.py     │  │ email.html を取得  │
│  ├─ score.py     │  │                    │
│  ├─ summarize.py │  │  ↓                 │
│  └─ render.py    │  │ GmailApp.sendEmail │
│  ↓               │  │  ↓                 │
│ output/          │  │ Outlook 受信トレイ │
│  ├─ index.html   │  │                    │
│  └─ email.html   │  │                    │
│  ↓               │  │                    │
│ GitHub Pages     │←─┤ HTML を取得        │
└──────────────────┘  └────────────────────┘
```

GitHub Actions と GAS は **疎結合**。共通の受け渡し場所は GitHub Pages 上の `email.html`。

## 本番情報

| 項目 | 値 |
|------|-----|
| リポジトリ | https://github.com/yuuukimachida-lang/legal-update-system (public) |
| Web版 | https://yuuukimachida-lang.github.io/legal-update-system/ |
| メール版 | https://yuuukimachida-lang.github.io/legal-update-system/email.html |
| 配信先 | yuki.machida@hamiltonlocke.com.au |
| cron (Actions) | 毎週月曜 07:00 JST (`0 22 * * 0`) |
| trigger (GAS) | 毎週月曜 07:00–08:00 JST のどこか |
| GAS プロジェクト | "Legal Update Mailer" (script.google.com) |
| Anthropic Secret | GitHub Secrets の `ANTHROPIC_API_KEY` |

## 仕様の正本

設計の根拠を変えた時は以下を更新する:

- `sources.yaml` — RSSソース定義
- `importance_rules.yaml` — 重要度判定ルール / final_score の閾値
- `templates/digest.html.j2` — Web 版テンプレート (Tailwind CDN, リッチ)
- `templates/digest_email.html.j2` — メール版テンプレート (table, インライン CSS)
- `email-mockup.html` / `deploy/index.html` — UI モック

## ローカル開発

```powershell
# .env に ANTHROPIC_API_KEY を設定 (.env.example をコピー)
pip install -r requirements.txt
python src/main.py     # 全パイプラインを通す (output/ に index.html, email.html が出る)
python src/render.py   # render だけ走らせる (summarized.json は事前に必要)
```

## デプロイ / 実行

| 操作 | 手段 |
|------|------|
| コード変更を反映 | `git push` のみでは Actions は起動しない。Actions タブで "Run workflow" を手動トリガー |
| メール配信を再送 | GAS で `sendDigest` を手動実行 (Actions のデプロイ完了後) |
| Secret 更新 | `(Get-Content .env \| Where-Object { $_ -match "^ANTHROPIC_API_KEY=" }) -replace "^ANTHROPIC_API_KEY=", "" \| % { $_.Trim() } \| gh secret set ANTHROPIC_API_KEY` |

## Gotchas (ハマりポイント)

### M365 / Outlook はメールに対して厳格
- `<script>` を含む HTML は **無音破棄** される。迷惑メールフォルダにも届かない
- 外部 CSS / Web フォント / `<link rel="stylesheet">` も同様にスパム判定の温床
- → メール用は table レイアウト + インラインスタイル + script ゼロ で組む (`digest_email.html.j2`)
- Why: 法律事務所のメール環境はクライアント情報保護のため特に厳しい

### Web 版とメール版を分けている理由
- 同じ HTML を両用するとどちらかが妥協になる (Web リッチ ↔ メール厳格)
- `render.py` で同一 context から 2 系統レンダリング。データとプレゼンの分離

### GitHub Actions は push で起動しない
- `weekly.yml` のトリガーは `schedule` と `workflow_dispatch` のみ。意図的
- Why: コード修正のたびに Claude API を呼ぶと無駄。実行タイミングは人間が制御する

### GAS の時間トリガーは 1 時間粒度まで
- 「月曜 7:30」は指定不可。「7-8 時」のどこかになる
- Actions が 07:00 開始で約 2 分で完了するため、GAS が 7-8 時枠なら確実に間に合う

### gh secret set の罠
- PowerShell の hidden input モードで Ctrl+V が効かないことがある
- → `Get-Clipboard | gh secret set ...` または `.env` から直接パイプする方式が確実
- 設定成功メッセージが出ても **値が空 / 不正** の可能性。動作確認まで含めて検証する

### `.gitignore` は追跡開始前に書く
- 既に追跡されているファイルには効かない
- 後から追加した時は `git rm --cached <path>` で追跡解除も必要
- 本プロジェクトでは `.env` (APIキー) と `memory/` (Claude Code 内部メモ) を除外

## トラブルシューティング

**メールが届かない**
1. GAS の実行ログを確認 (`送信完了:` が出ているか)
2. Outlook の迷惑メールフォルダ / M365 Quarantine を確認
3. Pages URL (`/email.html`) を直接ブラウザで開いて HTML が存在するか確認
4. `<script>` などが混入していないか HTML を目視確認

**Actions が失敗する**
1. Actions タブから失敗ジョブのログを開く
2. `Run pipeline` ステップの出力を確認 — エラーは Python のスタックトレースに出る
3. 401 / `invalid x-api-key` → Secret が空 or 不正。再登録する
4. RSS 取得失敗は一時的問題が多い。再実行で直ることが多い

**Pages が更新されない**
- Pages のデプロイは Actions の `deploy` ジョブ完了後。数十秒のラグあり
- ブラウザのキャッシュも疑う (Ctrl+F5 で強制リロード)
