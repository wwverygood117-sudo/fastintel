#!/usr/bin/env bash
# 自链式定时器：本轮跑完后补齐间隔，再触发下一轮。
# 触发凭据优先用 Actions 自带的 GITHUB_TOKEN（GH_TOKEN），
# 若仓库里额外配了 GH_PAT 也能用（GH_PAT 优先级更高，方便排障时替换）。
# 失败时会通过 Bark 给手机发一条告警，避免链断了没人知道。
set -uo pipefail

INTERVAL="${INTERVAL_SEC:-600}"
NOW=$(date +%s)
START="${JOB_START:-$NOW}"
SPENT=$(( NOW - START ))
WAIT=$(( INTERVAL - SPENT ))

# 留 20 秒余量，避免刚好卡在边界上导致两轮贴太近
if [ "$WAIT" -lt 20 ]; then WAIT=20; fi
if [ "$WAIT" -gt "$INTERVAL" ]; then WAIT=$INTERVAL; fi

echo "本轮已耗时 ${SPENT} 秒，等待 ${WAIT} 秒后续上下一轮"

TOKEN="${GH_PAT:-${GH_TOKEN:-}}"
if [ -z "$TOKEN" ]; then
  echo "::warning::既没有 GH_PAT 也没有 GITHUB_TOKEN，本轮无法续链。"
  exit 0
fi

sleep "$WAIT"

URL="https://api.github.com/repos/${GITHUB_REPOSITORY}/actions/workflows/monitor.yml/dispatches"

notify_fail() {
  if [ -n "${BARK_KEY:-}" ]; then
    curl -s --max-time 10 \
      "https://api.day.app/${BARK_KEY}/%E5%B7%A1%E6%A3%80%E9%93%BE%E6%96%AD%E4%BA%86/%E5%88%B0%20GitHub%20Actions%20%E9%87%8C%E6%89%8B%E5%8A%A8%E8%B7%91%E4%B8%80%E6%AC%A1%E5%B0%B1%E8%83%BD%E6%81%A2%E5%A4%8D?level=timeSensitive&group=fastintel" \
      >/dev/null 2>&1 || true
    echo "已通过 Bark 发出断链告警"
  fi
}

for i in 1 2 3 4; do
  CODE=$(curl -s -o /tmp/dispatch_resp.json -w "%{http_code}" --max-time 20 -X POST "$URL" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -d '{"ref":"main","inputs":{"mode":"normal"}}')

  echo "第 ${i} 次触发 -> HTTP ${CODE}"

  if [ "$CODE" = "204" ]; then
    echo "✅ 下一轮已排队，链路正常"
    exit 0
  fi

  if [ -s /tmp/dispatch_resp.json ]; then
    echo "响应体：$(head -c 400 /tmp/dispatch_resp.json)"
  fi

  # 401/403 是凭据失效或权限不足（多半是漏了 permissions: actions: write），重试没意义
  if [ "$CODE" = "401" ] || [ "$CODE" = "403" ]; then
    echo "::error::触发被拒（HTTP ${CODE}），检查 workflow 里是否声明了 permissions: actions: write"
    notify_fail
    exit 1
  fi

  sleep 15
done

echo "::error::续链连续 4 次失败，定时任务已中断"
notify_fail
exit 1
