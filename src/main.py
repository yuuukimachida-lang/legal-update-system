"""
週次バッチランナー: fetch → score → summarize → render を順に実行する。

各ステップは個別実行も可能 (デバッグ・部分再実行のため)。
このランナーは GitHub Actions の cron から呼ばれる想定。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

STEPS = [
    ("fetch.py", "RSS取得"),
    ("score.py", "重要度判定"),
    ("summarize.py", "要約生成"),
    ("render.py", "HTML生成"),
]


def run_step(script: str, label: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {label} ({script})")
    print('='*70)
    start = time.time()
    result = subprocess.run(
        [sys.executable, str(SRC / script)],
        cwd=ROOT,
    )
    elapsed = time.time() - start
    if result.returncode != 0:
        print(f"\n[error] {script} が失敗しました (exit={result.returncode})")
        sys.exit(result.returncode)
    print(f"\n  [{elapsed:.1f}s]")


def main() -> None:
    print(f"Legal Update System - 週次実行開始")
    total_start = time.time()

    for script, label in STEPS:
        run_step(script, label)

    total = time.time() - total_start
    print(f"\n{'='*70}")
    print(f"  完了 (合計 {total:.1f}秒)")
    print(f"  → output/index.html")
    print('='*70)


if __name__ == "__main__":
    main()
