#!/usr/bin/env bash
# patches/*.patch 를 이름 순서대로 git am 으로 적용하고 push.
# 워크플로 파일(.github/workflows)을 건드리는 패치는 GitHub 기본 토큰 권한상 자동 적용할 수 없어서 거부함.
set -uo pipefail
shopt -s nullglob
files=(patches/*.patch)
if [ ${#files[@]} -eq 0 ]; then echo "적용할 패치 없음"; exit 0; fi

git config user.name "trade-journal-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

work="$(mktemp -d)"
cp "${files[@]}" "$work/"
git rm -q "${files[@]}"
git commit -qm "patch: 업로드된 패치 파일 정리"

ok=0
for f in $(ls "$work"/*.patch | sort); do
  name="$(basename "$f")"
  if grep -q '^diff --git a/.github/workflows/' "$f"; then
    echo "::error::$name 은(는) 워크플로 파일을 바꿔서 자동 적용할 수 없어요. README '패치 수동 적용' 참고"
    exit 1
  fi
  if git am --3way --committer-date-is-author-date "$f"; then
    echo "적용 완료: $name"; ok=$((ok+1))
  else
    git am --abort || true
    echo "::error::$name 적용 실패 — 이미 적용된 패치이거나, 해당 파일을 직접 수정한 상태예요"
    exit 1
  fi
done

for i in 1 2 3; do
  git pull --rebase origin "${GITHUB_REF_NAME:-main}" && git push && { echo "패치 ${ok}개 반영"; exit 0; }
  sleep 3
done
exit 1
