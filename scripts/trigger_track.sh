#!/usr/bin/env bash
# 외부 크론(cron-job.org 등) 또는 로컬에서 Actions를 정시에 깨울 때 사용.
#
# 필요:
#   1) GitHub Fine-grained PAT
#        - Repository access: ybs8625-cmd/customs-tracker
#        - Permissions: Contents=Read, Actions=Read and write, Metadata=Read
#   2) export GITHUB_TOKEN=ghp_...
#
# cron-job.org 설정 예:
#   URL:     https://api.github.com/repos/ybs8625-cmd/customs-tracker/dispatches
#   Method:  POST
#   Headers: Authorization: Bearer <PAT>
#            Accept: application/vnd.github+json
#            X-GitHub-Api-Version: 2022-11-28
#            Content-Type: application/json
#   Body:    {"event_type":"track-ping"}
#   Schedule: every 5~10 minutes

set -euo pipefail

REPO="${TRACK_REPO:-ybs8625-cmd/customs-tracker}"
EVENT_TYPE="${TRACK_EVENT_TYPE:-track-ping}"
TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"

if [[ -z "${TOKEN}" ]]; then
  echo "GITHUB_TOKEN (또는 GH_TOKEN) 이 필요합니다." >&2
  exit 1
fi

curl -fsS -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "Content-Type: application/json" \
  "https://api.github.com/repos/${REPO}/dispatches" \
  -d "{\"event_type\":\"${EVENT_TYPE}\"}"

echo "dispatched ${EVENT_TYPE} -> ${REPO}"
