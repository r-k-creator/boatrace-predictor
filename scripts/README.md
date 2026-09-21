# scripts/ の見取り図

**直下(フラット)= 毎日の自動運用で使うもの+共有モジュール**。ワークフローが直接呼ぶので、
場所を動かすとGitHub Actionsが壊れます(`common.py`などを互いに`import`しているため)。

| 分類 | ファイル |
|---|---|
| 共通 | `common.py`(パス定数・API取得・JST時刻) |
| 朝: 出走表・予想 | `fetch_program.py` `predict.py` `refresh_near_race.py` `recheck_program.py` |
| 候補→買い目→メール | `generate_bets.py` `build_candidates_email.py` `deadline_reminder.py` `fetch_odds.py` |
| 夜: 結果・学習・評価 | `fetch_results.py` `build_stats.py` `evaluate.py` `axes.py` `train_model.py` `evaluate_candidates.py` `evaluate_bets.py` |
| 検証 | `backtest.py`(過去日をさかのぼるウォークフォワード検証。`analysis/`から使われる) |
| 補助 | `fetch_previews.py` `git_push_with_retry.sh` |

**`analysis/` = 単発の分析・シミュレーション**(手動実行。他から呼ばれない)
`analyze_*.py`(原因分析・偏りの調査)、`simulate_*.py`(Sランク全賭け・1号艇ベースラインのシミュレーション)。

**`tools/` = 手動のデータ操作**(cronには入れない)
`backfill.py`(過去分の一括取得)、`backfill_exotic_payouts.py`(払戻の穴埋め)、
`backfill_odds.py`(過去の確定オッズ)、`migrate_csv_schema.py`(CSVスキーマ移行)。

`analysis/`・`tools/`のスクリプトは、先頭で`scripts/`をimportパスに足しているため、どのフォルダからでも
`python scripts/tools/backfill_odds.py`のように実行できます。
