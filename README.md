# ボートレース予想ツール(毎日自動データ収集・集計)

プログラミング未経験でも GitHub Actions を使って「毎日自動でデータを集め、
簡単な予想スコアを計算してCSVで出力する」仕組みを動かせるようにしたプロジェクトです。

## できること / できないこと

**できること**
- 毎日 10:00 (日本時間) に自動実行、以下を毎日繰り返す完全なループになっています

  1. **結果取得** … 前日のレース結果を [Boatrace Open API](https://github.com/BoatraceOpenAPI)(非公式・無料)から取得し `data/` にCSVで蓄積
  2. **統計更新** … 蓄積データから「コース番号別の勝率」「選手別の勝率」を再計算(`data/stats/`)
  3. **振り返り** … 前日の予想(`predictions/`)と実際の結果を突き合わせ、的中率などを `data/evaluations.csv` に記録
  4. **学習** … 蓄積された出走表データ+結果データで scikit-learn のロジスティック回帰を再学習し、`data/stats/model.json` を更新
  5. **予想** … 当日の出走表を取得し、学習済みモデル(データが十分無ければシンプルな加重平均式にフォールバック)で予想スコアを計算し `predictions/{日付}.csv` に出力

- **さらに、5分おき(日本時間9:00〜23:55)に別のワークフローが動き、締切が近いレースだけを直前情報
  (実際の進入コース・展示タイム)で再計算します**(`scripts/refresh_near_race.py` / `.github/workflows/near_race.yml`)。
  出走表の艇番号ではなく、previews APIが返す**実際の進入コース**を使うので、朝の予想より精度が上がります。

**できないこと(重要)**
- **リアルタイムのオッズは取得していません。** このAPIにはオッズが含まれないため、
  「予想スコアと実際のオッズを見比べて妙味があるか判断する」のは公式サイトやテレボートで
  ご自身の目で確認する必要があります。
- ここで使っている予想スコアは機械学習モデル(データが50レース分溜まれば自動でロジスティック回帰に切り替わります)か、
  透明性重視のシンプルな加重平均(それまでのフォールバック)のどちらかです。
- 「締切ちょうど5分前」を秒単位で保証することはできません。GitHub Actionsの無料スケジュール実行は
  数分単位でタイミングがずれることがあるため、`refresh_near_race.py` は「締切前後、幅を持たせたウィンドウ」の
  レースをまとめて処理する設計にしています。

### リポジトリはPublicがおすすめ
`near_race.yml` は5分おきに1日中動くため、実行時間の積み重ねがそれなりの量になります。
GitHubはPublicリポジトリならActionsの実行時間が無料(無制限)ですが、Privateだと無料枠に上限があります。
レース予想データに個人情報は含まれないので、特にこだわりが無ければPublicで作成することをおすすめします。
Privateにしたい場合は、`.github/workflows/near_race.yml` の `cron: "*/5 ..."` を `*/10` や `*/15` に
変更して実行頻度を落とせば、無料枠に収まりやすくなります。

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

### 4. 手動で一度動かしてみる
1. リポジトリの「Actions」タブを開く
2. 左側の「Daily boatrace data & predictions」を選択
3. 右側の「Run workflow」ボタン→「Run workflow」で手動実行
4. 数十秒〜数分待ち、緑のチェックマークが付けば成功

成功すると `data/` と `predictions/` フォルダにファイルが追加されているはずです。

### 5. あとは毎日自動で動きます
`.github/workflows/daily.yml` の設定により、毎日 10:00(JST)に自動実行されます。
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
`train_model.py` は **最低50レース分の結果データが溜まるまで学習をスキップ**します(データが少なすぎると
偶然に左右されたモデルになってしまうため)。それまでは `predict.py` は自動的にシンプルな加重平均式に
フォールバックします。運用開始から1〜2週間ほど経つと自然に学習済みモデルに切り替わります。

CSVはGitHub上でそのまま見られますし、「Download raw file」でダウンロードしてExcelで
開くこともできます。以前お話しした「コース別回収率」「決まり手別」などの分析は、
`data/results_entries.csv` と `data/results_races.csv` を使ってそのまま実践できます。

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
