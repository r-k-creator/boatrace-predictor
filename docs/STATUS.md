# STATUS(入り口ドキュメント)

新しいセッションはまず**このファイルだけ**読めば全体像を把握できるようにしています。
経緯・詳細な設計判断は書きません(→ [docs/betting_system_design.md](betting_system_design.md)、
[README.md](../README.md)、[backtest_conclusion.md](../backtest_conclusion.md)を参照)。

## システム構成の概要

競艇(ボートレース)の出走表・直前情報・結果をBoatrace Open API(非公式)から毎日自動取得し、
ロジスティック回帰モデル(最低100レース分溜まるまでは加重平均にフォールバック)で各レースの
勝率を予測、GitHub ActionsのCron+外部のCowork「Routine」タスクが候補レースを選定して
`data/latest/candidates.json`に書き出す。そこからランク(SS/S/A/B/C)・予算・具体的な買い目
(2連単/3連単/3連複、Harville公式ベース)を自動生成してメール通知し、翌日以降に実際の結果と
突き合わせて的中率・収支を評価、`data/latest/`に蓄積する仕組み。オッズは取得しておらず、
実際に賭けるかどうかの最終判断とオッズ確認は人間が行う。

## 稼働中のワークフロー一覧

| 時刻(JST) | ワークフロー | 役割 |
|---|---|---|
| 8:00 | `morning_program.yml` | 当日の出走表取得(`fetch_program.py`)・予想スコア計算(`predict.py`) |
| 9:00 | `candidates_insurance_check.yml` | Cowork Routine(8:15頃`data/latest/candidates.json`を生成)が失敗していないかの保険チェック |
| (随時) | `candidates_notify.yml` | `data/latest/candidates.json`のpushをトリガーに、ランク/買い目を計算しメール送信 |
| 13:00 | `programs_recheck.yml` | 直前の選手変更(乗り替わり)差異チェック、あればメール |
| 22:03 | `evening_results.yml` | 結果取得・モデル再学習・候補評価・買い目評価・収支メール送信 |
| 23:07 | `evening_results_retry.yml` | 22:03の実行が失敗/未取得だった場合のリトライ(冪等) |
| (`claude/**` push時) | `auto_merge_data_branches.yml` | 自動生成データのみのブランチをレビュー無しでmainへfast-forward |

## data/ フォルダの構成(2026-09-18整理)

- `data/archive/` … 過去の生データ・大容量の蓄積/分析結果(programs/previews/results_*、
  evaluations.csv、backtest_*、stats/axes、stats/backtest_axes_*)。普段のセッションでは
  参照不要。
- `data/latest/` … 直近の状態・日常的に参照するもの(candidates.json、
  candidates_evaluations.csv、bets_evaluations.csv、stats/model.json、stats/course_stats.csv、
  stats/racer_stats.csv)。

パス定数は`scripts/common.py`に集約されており(`ARCHIVE_DIR`/`LATEST_DIR`/`STATS_DIR`/
`ARCHIVE_STATS_DIR`等)、ほとんどのスクリプトはここ経由でパスを解決する。

## 直近の既知の課題・保留事項

- **外部のCowork Routineタスク(このリポジトリの外)が要更新**: 毎朝`data/candidates.json`
  ではなく`data/latest/candidates.json`に書き込むよう変更が必要(2026-09-18のdata/フォルダ
  再編に伴う)。未対応だと翌朝の候補生成が壊れる。
- **SSランクの閾値(0.80)は暫定値**(n=59の薄いサンプル)。Stage 2の較正チェックで最優先
  再検証すること。詳細: [docs/betting_system_design.md](betting_system_design.md)。
- **Stage 2(学習機能: 較正チェック・累積収支・予算自動調整)は未着手**。Stage 1(結果取得+
  日次収支メール)が実データで安定稼働したと判断されてから着手する。
- **`evening_results.yml`/`evening_results_retry.yml`以外のcronは分=0のまま**
  (`morning_program.yml`等)。GitHub Actionsの毎時ちょうどの混雑による数時間遅延リスクが
  残っている(evening系2つは2026-09-17に対応済み)。
- **月1回程度の「柔らかい注意喚起表現」目視レビュー**(ただし/一方で等、キーワード判定で
  拾えないもの)は未実装、必要になったら着手。
- **`data/latest/candidates_evaluations.csv`/`bets_evaluations.csv`は日次追記で際限なく増える**
  (`results_races.csv`と同じ性質)。肥大化したら`archive/`への移動を再検討する。
