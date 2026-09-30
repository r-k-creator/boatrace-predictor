# STATUS(入り口ドキュメント)

新しいセッションはまず**このファイルだけ**読めば全体像を把握できるようにしています。
経緯・詳細な設計判断は書きません(→ [docs/betting_system_design.md](betting_system_design.md)、
[README.md](../README.md)、[backtest_conclusion.md](backtest_conclusion.md)を参照)。

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
| 22:03 | `evening_results.yml` | 結果取得・モデル再学習・候補評価・買い目評価・オッズ精度チェック・EVティア評価・収支メール送信 |
| 23:07 | `evening_results_retry.yml` | 22:03の実行が失敗/未取得だった場合のリトライ(冪等) |
| 23:25 | `evening_results_insurance_check.yml` | 22:03・23:07の後、本日分の収支メールが実際に送れたか(`data/latest/bets_evaluations.csv`の本日更新)の保険チェック |
| (外部スケジューラからのworkflow_dispatchのみ) | `deadline_reminder.yml` | 候補レース1件ごとに短い通知メール。買い目はEVティア判定(3連単)の結果で、EV対象なしは「見送り」、オッズ未取得は「判定不可」と明示し、旧ランク基準は参考として末尾に表示(2026-09-27〜)(送信済みは`data/latest/deadline_reminders_sent.json`で管理)。5分おきcron(schedule:)は2026-09-28に無効化済み(下記「解決済み」参照)、`workflow_dispatch`の`race_key`指定でのみ起動する |
| 8:04〜21:59(5分おき) | `odds_fetch.yml` | 候補(SS/S/A)の締切5〜15分前に公式サイトからオッズ(2連単/2連複/3連単/3連複)を取得し`data/latest/odds/`に保存(蓄積に加え、`deadline_reminder.yml`のEVティア判定・`evening_results.yml`のEV評価に使用) |
| (`claude/**` push時) | `auto_merge_data_branches.yml` | 自動生成データのみのブランチをレビュー無しでmainへfast-forward |

## フォルダ構成(2026-09-23整理)

```
boatrace-predictor/
├─ CLAUDE.md / README.md   … 入り口(新しいセッションはSTATUS.mdから)
├─ docs/                   … STATUS.md(これ)・betting_system_design.md(設計判断ログ)・backtest_conclusion.md
├─ .github/workflows/      … 自動実行のワークフロー一式
├─ scripts/                … 毎日の自動運用で使うPython(直下)。中身の地図は scripts/README.md
│   ├─ analysis/           … 単発の分析・シミュレーション(手動)
│   └─ tools/              … 手動のデータ操作(バックフィル・移行)。cronには入れない
├─ data/
│   ├─ latest/             … 直近の候補・評価・モデル・オッズ
│   └─ archive/            … 過去の生データ・大容量の蓄積
│       └─ analysis_outputs/ … 単発の調査・シミュレーション成果物(日次パイプラインの生データとは別置き)
└─ predictions/            … 日々の予想(ワークフローが直接読み書きするので据え置き)
```

## data/ フォルダの構成(2026-09-23整理)

- `data/archive/` … 過去の生データ・大容量の蓄積/分析結果(programs/previews/results_*、
  evaluations.csv、backtest_*、stats/axes、stats/backtest_axes_*)。普段のセッションでは
  参照不要。
  - `data/archive/analysis_outputs/` … 一回限りの調査・シミュレーションの成果物
    (`backtest_full_probs_b.csv`、`odds_target_60days.csv`、`odds_backfill_60days.csv`、
    `odds_backfill_60days_exacta.csv`、`s_rank_simulation_bets.csv`)。日次パイプラインが
    常に更新する`archive/`直下の生データとは性質が違うため分離(2026-09-23)。
    パス定数は`scripts/common.py`の`ANALYSIS_OUTPUTS_DIR`。
- `data/latest/` … 直近の状態・日常的に参照するもの(candidates.json、
  candidates_evaluations.csv、bets_evaluations.csv、odds/(締切直前オッズのスナップショット)、
  odds_accuracy.csv(締切直前オッズと確定払戻の精度記録、2026-09-23追加)、
  ev_tier_evaluations.csv(EVティア方式の成績記録、2026-09-23追加)、stats/model.json、
  stats/course_stats.csv、stats/racer_stats.csv)。

パス定数は`scripts/common.py`に集約されており(`ARCHIVE_DIR`/`LATEST_DIR`/`STATS_DIR`/
`ARCHIVE_STATS_DIR`等)、ほとんどのスクリプトはここ経由でパスを解決する。

## 直近の既知の課題・保留事項(2026-09-23、優先度順に整理)

このセッションには会話をまたぐ記憶が無いため、新しいセッションは必ずこのリストから
着手すること。優先度・状況は今後の作業で随時更新する。

### 進行中(codeに変更を渡し済み・main未反映)

(現在、進行中の項目はありません)

### 最優先

1. **予想士を「時間確認→オッズ確認→EV計算→参加レース判定→通知・買い目反映」の
   一本化フローに再設計する**。ただし全レース一気にではなく、まずSS/S/A範囲で試してから
   対象を広げる段階を踏む。EVティア方式(3連単限定、下記「解決済み」のA参照)の
   運用そのものに関わる設計変更のため優先度最上位。
2. **オッズ取得がほぼ機能しておらず、EVティア判定の前提が崩れている**: 予想士の検証
   (2026-09-27、[PR #2](https://github.com/r-k-creator/boatrace-predictor/pull/2))で判明。
   直近31候補レース中30レースで締切前オッズが`data/latest/odds/`に残っておらず(1件のみ)、
   締切メール自体もGitHubの5分おきcronが間引かれているらしく、候補6〜13件/日のうち1〜2件
   しか送られていない。加えて`fetch_odds.py`はB/Cランクを対象外にしている。この状態では、
   予想士が通知する買い目をEVティア判定へ置き換えても(PR #2)、実運用ではほとんどが
   「オッズ未取得(判定不可)」表示になり、最優先1番の「予想士の一本化」が実質的に
   機能しない。原因切り分け(cronが本当に間引かれているのか/実行時間や負荷の問題か)と
   対策が必要。

### 次点

3. **Bランクの3連単オッズをバックフィル**(3,850件・約11時間、Codeで実行)→
   SS/S/A/Bまで対象を広げたEVティア方式の結果・グラフを確認する。
4. **EVティア方式(3連単限定の賭け金ルール)のしきい値・金額(EV 2.0/3.0、3,000円/6,000円)・
   EV計算の対象点数上限(上位8点)は60日間限定のチューニング値**(2026-09-23実装、
   同日にEV計算の対象を全120通り→上位8点に訂正、詳細は
   [docs/betting_system_design.md](betting_system_design.md)「EVティア方式の実装」
   「スコープ訂正」)。`data/latest/ev_tier_evaluations.csv`にデータが蓄積されてきたら、
   定期的に人と相談して見直すこと(自動でしきい値・金額・対象点数を書き換える仕組みは無い)。
5. **データ監視の仕組み(結果取り違え・紐付けミス・オッズ異常・学習データ異常混入の自動検知)は
   設計途中で止まっている**: (1)朝/レース前/夜の各チェック項目のうちどれを先に実装するか、
   (2)「直せるものは自動で取り直す、直せない異常は`train_model.py`を止めて通知する」方針の
   具体的な振り分け、(3)既存のGitHub Actions(cron)に組み込むか別基盤にするか、の3点が
   未決定。(3)は(1)(2)が固まってから着手。
6. **SSランクの閾値(0.80)は暫定値**(n=59の薄いサンプル)。Stage 2の較正チェックで最優先
   再検証すること。詳細: [docs/betting_system_design.md](betting_system_design.md)。

### 相談・分析待ち

7. **大穴でモデルの確率が甘くなる問題の補正**(未着手)。2連単がEVティア方式でうまく
   機能しなかった原因(フェイバリット・ロングショット・バイアス)とも関連。
8. **「午後の候補が偏る」件の調査**(未着手、詳細は今後詰める)。
9. **Stage 2(学習機能: 較正チェック・累積収支・予算自動調整)は未着手**。Stage 1(結果取得+
    日次収支メール)が実データで安定稼働したと判断されてから着手する。

### 優先度低め

10. **`data/latest/candidates_evaluations.csv`/`bets_evaluations.csv`は日次追記で際限なく増える**
    (`results_races.csv`と同じ性質)。肥大化したら`archive/`への移動を再検討する。
11. **月1回程度の「柔らかい注意喚起表現」目視レビュー**(ただし/一方で等、キーワード判定で
    拾えないもの)は未実装、必要になったら着手。
12. **自動マージ失敗時のPR自動作成の再確認**(`auto_merge_data_branches.yml`のフォールバック
    経路、動作未検証)。

### 解決済み(参考、記述はここから削除)

- ~~9/20結果データの空欄バグの後始末~~ → 2026-09-30に対応完了。09-20分は
  `results_races.csv`の再取得(2026-09-22、PR #3)後、`data/latest/bets_evaluations.csv`
  への追記が未実施のまま残っていたが、git履歴上の当日の候補一覧+
  `predictions/20260920.csv`から`generate_bets.py`で当時と同じロジックによりrank/budget/betsを
  再構成し、`evaluate_bets.py`を手動実行して追記した(投資32,000円・回収28,180円・
  回収率88.1%、以前記録していた「正しい数字」と完全一致)。09-29分も同じ手順で対応
  (投資29,000円・回収36,850円・回収率127.1%)。09-18分は該当日の`candidates.json`が
  git履歴上どこにも見当たらず(コミットされた形跡が無い)、再構成できないため対応不可。
  09-21分は`results_races.csv`に35レース分(156中)結果未確定が残っており、これらが
  解消されない限り追記できない(未解消のまま)。
- ~~`deadline_reminder.yml`の5分おきcron(schedule:)~~ → 2026-09-28に無効化(コメントアウト、
  `workflow_dispatch`は維持)。外部スケジューラ(Windows タスクスケジューラによる、締切5分前
  ちょうどの正確な1回だけの起動、`race_key`指定の`workflow_dispatch`経由)が本番運用で実証
  されたため。旧cronを並行稼働させたままだと、間引かれて遅れて発火したcronが「オッズ未取得
  の通知」を先に送信済みとして記録してしまい、その後に外部スケジューラから来る正しい
  (オッズ付きの)通知がスキップされる実害が発生した(2026-09-28 13:40 戸田7R)。
  **既知のトレードオフ**: 外部スケジューラ側のPCが起動していない・スリープ中は通知が
  一切送られなくなる(旧cronのような「GitHub側だけで完結する保険」が無くなる)。これは
  Windows タスクスケジューラ方式を採用した時点で既に許容している制約であり、新たに
  発生したものではない。`odds_fetch.yml`側の5分おきcronは重複送信の問題が起きないため、
  安全網としてそのまま維持している。
- ~~A. 予想士(外部Cowork Routine)へのEVベース買い目・予算ロジック反映~~ →
  実際には外部Cowork Routineへの依頼ではなく、別セッションによる**このリポジトリ内**の
  実装だった。`scripts/deadline_reminder.py`の`build_message()`を変更し、締切直前メールの
  件名・本文をEVティア判定(3連単、確率上位8点)の結果に一本化、旧ランク基準の買い目は
  参考として本文末尾に残す形で実装([PR #2](https://github.com/r-k-creator/boatrace-predictor/pull/2)、
  2026-09-27マージ)。`generate_bets.py`や朝の候補選定、他の評価スクリプトへの変更はない。
  検証記録は[docs/verification/2026-09-27_deadline_reminder_ev_final.md](verification/2026-09-27_deadline_reminder_ev_final.md)。
  ただし、この変更の前提となるオッズ取得自体がほぼ機能していない問題が検証中に判明し、
  別途「最優先」3番として記録している。
- ~~`backtest.py`の環境変数対応パッチ~~ → `BACKTEST_VARIANTS`/`BACKTEST_FULL_PROBS_MIN`/
  `BACKTEST_FULL_PROBS_OUT`は`scripts/backtest.py`に反映済み(2026-09-23、「結果未確定
  データの永続スキップバグ」修正の一部としてこの会話内でマージ済み)。
- ~~`evening_results_insurance_check.yml`パッチ~~ → 既に存在・稼働中(上記「稼働中の
  ワークフロー一覧」参照)。
- ~~外部のCowork Routineタスクが`data/candidates.json`に書き込んでいる~~ →
  2026-09-19以降`data/latest/candidates.json`へ正しく書き込まれ続けていることをgit履歴で
  確認済み(2026-09-23)。当時は2026-09-18のdata/フォルダ再編に伴う要対応事項として
  記載していたが、既に解消している。
- ~~`evening_results.yml`/`evening_results_retry.yml`以外のcronは分=0のまま~~
  **2026-09-21対応済み**: `morning_program.yml`(8:02 JST)・`candidates_insurance_check.yml`
  (9:03 JST)・`programs_recheck.yml`(13:04 JST)も分=0から数分ずらした
  (`deadline_reminder.yml`/`odds_fetch.yml`はもともと分ずらし済み)。
