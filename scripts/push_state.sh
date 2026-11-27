#!/usr/bin/env bash
# Commit staged feed/state changes and push to main, surviving concurrent
# updates. During a rebase, stage 3 ("theirs") is our own commit, so a conflict
# in state/seen/feed files is resolved by keeping our version.
set -u
MSG="$1"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A
if git diff --cached --quiet; then exit 0; fi
git commit -m "$MSG" || exit 1

for attempt in $(seq 1 20); do
  git rebase --abort 2>/dev/null || true
  if git pull --rebase origin main; then
    if git push origin main; then exit 0; fi
  else
    files=$(git diff --name-only --diff-filter=U)
    if [ -n "$files" ]; then
      # shellcheck disable=SC2086
      git checkout --theirs -- $files 2>/dev/null || true
      git add -A
      if GIT_EDITOR=true git rebase --continue >/dev/null 2>&1; then
        if git push origin main; then exit 0; fi
      else
        # Our commit became empty after resolving; upstream already has it.
        GIT_EDITOR=true git rebase --skip >/dev/null 2>&1 || true
        git rebase --abort 2>/dev/null || true
        exit 0
      fi
    fi
  fi
  git rebase --abort 2>/dev/null || true
  sleep 10
done
exit 1
