# ボートレース予想ツール(毎日自動データ収集・集計)

プログラミング未経験でも GitHub Actions を使って「毎日自動でデータを集め、
簡単な予想スコアを計算してCSVで出力する」仕組みを動かせるようにしたプロジェクトです。

## できること / できないこと

**できること**

GitHub Actionsが以下のワークフローを毎日自動実行する、完全なループになっています
(すべて日本時間 JST 基準)。

| 時刻(JST) | ワークフロー | 内容 |
|---|---|---|
| 8:00 | `morning_program.yml` | 当日の出走表を取得し(`fetch_program.py`)、予想スコアを計算(`predict.py`) |
| 9:00 | `candidates_insurance_check.yml` | Coworkの候補選定タスク(8:15頃)が`candidates.json`を更新したか確認、未更新ならワークフローを失敗させて気付けるようにする |
| (随時) | `candidates_notify.yml` | `candidates.json`がpushされたら、その内容をメールで送信 |
| 22:00 | `evening_results.yml` | 当日のレース結果を取得し(`fetch_results.py`)、統計更新・振り返り(`evaluate.py`)・モデル再学習(`train_model.py`)まで一気に実行 |
| 23:00 | `evening_results_retry.yml` | 22:00の実行が失敗/未取得だった場合のリトライ(各スクリプトは処理済みならスキップするため、22:00が成功していれば何も起きない) |

直前情報(previews)の自動巡回取得(旧`near_race.yml`、5分おき)は廃止しました。実際の進入コース・
展示タイム・オッズは、参加すると決めたレースについてのみ**スクリーンショットを撮って手動で確認する**
運用に変更しています(下記「参加レースの手動フロー」参照)。過去データのバックテスト用に直前情報を
アーカイブしたい場合は `scripts/fetch_previews.py` / `scripts/backfill.py` を手動実行してください。

**できないこと(重要)**
- **リアルタイムのオッズは取得していません。** このAPIにはオッズが含まれないため、
  「予想スコアと実際のオッズを見比べて妙味があるか判断する」のは公式サイトやテレボートで
  ご自身の目で確認する必要があります。
- ここで使っている予想スコアは機械学習モデル(データが100レース分溜まれば自動でロジスティック回帰に切り替わります)か、
  透明性重視のシンプルな加重平均(それまでのフォールバック)のどちらかです。

## セットアップ手順(コードは書きません)

### 1. GitHubに新しいリポジトリを作る
GitHubにログイン → 右上の「+」→「New repository」→ 適当な名前(例: `boatrace-predictor`)を付けて作成。
Public/Privateどちらでも構いません(データを他人に見られたくなければ Private)。

### 2. このフォルダの中身をアップロードする
一番簡単なのは **GitHub Desktop**(無料のアプリ、コマンド入力不要)を使う方法です。

1. [GitHub Desktop](https://desktop.github.com/) をインストールしてGitHubアカウントでログイン
2. 「File」→「Add local repository」で、解凍したこのフォルダを選択
3. 「Publish repository」ボタンで、先ほど作ったリポジトリに公開

(Web UIの「Add file → Upload files」でもアップロードできますが、フォルダ構成が
崩れやすいので、GitHub Desktopの方が確実です)

### 3. Actionsの書き込み権限を有効にする(重要・忘れやすいポイント)
1. リポジトリの「Settings」タブを開く
2. 左メニューの「Actions」→「General」
3. 一番下の「Workflow permissions」で **「Read and write permissions」** を選択して保存

これを忘れると、自動実行はされてもデータをリポジトリに書き戻せず失敗します。

### 4. candidates.json通知メール用のSecretsを登録する(`candidates_notify.yml`を使う場合)
1. リポジトリの「Settings」→「Secrets and variables」→「Actions」
2. 「New repository secret」で以下の3つを登録:
   - `GMAIL_USERNAME` … 送信元のGmailアドレス
   - `GMAIL_APP_PASSWORD` … Gmailの[アプリパスワード](https://support.google.com/accounts/answer/185833)
     (2段階認証を有効にした上で発行する。通常のログインパスワードは使えません)
   - `NOTIFY_EMAIL_TO` … 送信先メールアドレス(自分宛でも可)

`candidates.json`はまだCoworkの候補選定タスク(Phase 6)が作る前提のファイルです。それまではこの
ワークフローは何もトリガーされません。

### 5. 手動で一度動かしてみる
1. リポジトリの「Actions」タブを開く
2. 左側から「Morning program fetch & predict」を選択
3. 右側の「Run workflow」ボタン→「Run workflow」で手動実行
4. 数十秒〜数分待ち、緑のチェックマークが付けば成功

成功すると `data/` と `predictions/` フォルダにファイルが追加されているはずです。同様に
「Evening results fetch & retrain」も手動実行して結果取得側も確認しておくと安心です。

### 6. あとは毎日自動で動きます
`.github/workflows/` 配下の各ワークフローの設定により、上記の表の時刻(JST)に自動実行されます。
パソコンを開いていなくても、GitHub側のサーバーが実行してくれます。

## 結果の見方

- `predictions/{YYYYMMDD}.csv` … その日のレースごとの予想スコア・予想順位・使用モデル(`model_used`列)。
  締切が近づいて`refresh_near_race.py`が更新したレースは、`actual_course_number`(実際の進入コース)・
  `exhibition_time`(展示タイム)・`updated_at`(更新時刻)も入り、`model_used`列に「+直前情報」と付きます
- `data/results_races.csv` … レース単位の結果(天候・決まり手・払戻金など)。天候系カラムはAPIの
  生フィールド名をそのまま使っています(`race_wind` / `race_wind_direction_number` / `race_wave` /
  `race_weather_number` / `race_temperature` / `race_water_temperature` / `race_technique_number`)
- `data/results_entries.csv` … 出走艇単位の結果。`entry_course_actual`(実際の進入コース)・
  `entry_course_program`(出走表時点=艇番号ベースの想定進入コース)・`motor_2rate` / `motor_3rate`
  (モーター2/3連率、同日の出走表データから補完)を含みます
- `data/stats/course_stats.csv` … コース番号別の勝率・平均払戻金
- `data/stats/racer_stats.csv` … 選手別の勝率
- `data/stats/model.json` … 学習済みロジスティック回帰の係数(中身を見れば「何が勝敗に効いているか」がそのまま分かります)
- `data/evaluations.csv` … レース単位の振り返り結果(1レース=1行)。モデルが1位に予想した艇を主語に、
  その艇の実際の進入コース・スタートタイミング・モーター成績、レースの格(`grade`)・昼夜(`is_night`、
  締切時刻からの推定)・天候、的中結果(`hit_top1` / `hit_top2` / `brier_score`)などを記録します。
  カラムはバケット化(「風速帯:中」等)せず、APIの生の値をそのまま保存する方針にしており、
  集計・グラフ化は分析側(Excel・スクリプト)で行う想定です
- `data/stats/axes/*.csv` … `evaluate.py`実行のたびに蓄積済み全データから再計算される軸別集計
  (`scripts/axes.py`)。`single_*.csv`はモデルの確信度5分位・場・進入コース・決まり手・グレード・
  昼夜・天候・風速帯/風向/波高帯・モーター2連率帯ごとの的中率、`cross_*.csv`は選手×場×進入コース・
  選手×決まり手(勝った時の決まり手分布)・選手×展示タイム順位×着順・選手×スタートタイミング
  (平均+分散)・場×進入コース×風速帯/風向(イン逃げ率つき)などの掛け合わせ集計です。
  いずれもサンプル数が少ない行は`low_sample`列で1が立ちます(除外はせず参考値として残しています)

### 既存データのスキーマ移行

過去に運用して `data/results_races.csv` / `data/results_entries.csv` が旧カラム名(`wind` /
`course_number` 等)のまま残っている場合は、以下を一度だけ実行すると新スキーマに移行できます
(バックアップとして `*.csv.bak` を残します。複数回実行しても安全です)。

```bash
python scripts/migrate_csv_schema.py
```

### 学習が始まるタイミングについて
`train_model.py` は **最低100レース分の結果データが溜まるまで学習をスキップ**します(データが少なすぎると
偶然に左右されたモデルになってしまうため)。それまでは `predict.py` は自動的にシンプルな加重平均式に
フォールバックします。運用開始から数日〜1週間ほど経つと自然に学習済みモデルに切り替わります。

CSVはGitHub上でそのまま見られますし、「Download raw file」でダウンロードしてExcelで
開くこともできます。以前お話しした「コース別回収率」「決まり手別」などの分析は、
`data/results_entries.csv` と `data/results_races.csv` を使ってそのまま実践できます。

## バックテスト(ウォークフォワード検証)

`scripts/backtest.py` は、過去の各日について「その日より前のデータだけ」でモデルを
日次で再学習し直し、その日のレースを予想・評価するウォークフォワード検証です
(本番のtrain_model.pyと同じ日次粒度)。単純な「場ごとに過去最高勝率の進入コースを
常に予想する」ナイーブベースラインも同じ「過去のみ使用」ルールで計算し、モデルが
それを本当に上回っているかを比較できます。

対象期間(デフォルト2025-05-01〜)のデータが `data/programs/` `data/results_*.csv` に
揃っている必要があります。日次運用は「今日」「昨日」しか取得しないため、長期間を
検証するには先に一括取得が必要です:

```bash
cd scripts
python backfill.py 2025-05-01              # 2025-05-01〜今日まで一括取得(時間がかかります)
python backtest.py                          # 2025-05-01〜取得済みデータの最終日で検証
python backtest.py 2025-05-01 2025-08-31    # 期間を指定して検証
```

結果は `data/backtest_evaluations.csv`(レース単位)・`data/backtest_daily_summary.csv`
(日別サマリー、モデル vs ナイーブ)・`data/stats/backtest_axes/single_*.csv`
(Phase3と同じ軸でモデル/ナイーブを横並び集計)に出力されます。

## 手元(ローカルPC)で試したい場合

```bash
cd scripts
pip install -r ../requirements.txt

python fetch_results.py 2025-07-15   # 指定日の結果を取得
python build_stats.py                 # 統計を再計算
python evaluate.py 2025-07-15         # 振り返り(前日の予想と結果を突き合わせ)
python train_model.py                 # 学習(50レース分溜まったら自動でモデルを更新)
python fetch_program.py 2025-07-15    # 指定日の出走表を取得
python predict.py 2025-07-15          # 予想スコアを計算(学習済みモデルがあればそれを使用)
```

日付を省略すると「結果は昨日」「出走表は今日」を対象にします(自動実行時と同じ挙動)。

## 免責事項

- このツールは統計的な参考情報を提供するものであり、的中や回収率の向上を保証するものではありません。
- 使用しているAPIは非公式のコミュニティプロジェクトです。仕様変更や提供終了の可能性があります。
- 公式情報が必要な場合は必ず BOAT RACE公式サイト・テレボートをご確認ください。
