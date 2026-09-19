#!/usr/bin/env bash
# コミット済みの変更をmainへpushする(GitHub Actionsのワークフロー用)。
#
# 複数のワークフロー(morning_program / evening_results / auto_merge_data_branches /
# Cowork Routineなど)が同じmainへ近いタイミングでpushするため、単純な`git push`だと
# 「fetch first」で拒否されることがある(2026-09-18のmorning_program.ymlで実際に発生)。
# push前に`git pull --rebase origin main`で最新を取り込み、それでも失敗したら
# 数秒待って再試行する(最大3回)。リベースが衝突した場合は中断して再試行に回す。
# 3回とも失敗したら非ゼロで終了し、ワークフローを失敗させる。
set -u

MAX_ATTEMPTS=3
for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  if git pull --rebase origin main && git push; then
    echo "push succeeded (attempt ${attempt}/${MAX_ATTEMPTS})"
    exit 0
  fi
  echo "::warning::push failed (attempt ${attempt}/${MAX_ATTEMPTS})"
  git rebase --abort 2>/dev/null || true
  sleep $((attempt * 5))
done

echo "::error::push failed after ${MAX_ATTEMPTS} attempts"
exit 1
