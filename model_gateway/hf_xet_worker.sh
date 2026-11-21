#!/bin/bash
# hf_xet_worker.sh — one-file downloader for hf_xet_fetch supervisor.
# Minimal footprint (~6MB: bash + one zero-buffer curl), fast path only:
# unbounded --continue-at appends; never use bounded ranges (throttled).
# Usage: hf_xet_worker.sh URL DEST SIZE REV CONN_TIME
URL="$1"; DEST="$2"; SIZE="$3"; REV="$4"; CT="${5:-300}"
MARK="$DEST.complete"
# Downloads run in their own process group. End the group if the gateway dies.
if [ -n "${6:-}" ]; then
  owner="$6"; group="$$"
  (while kill -0 "$owner" 2>/dev/null; do sleep 2; done
   kill -TERM -- "-$group" 2>/dev/null) &
  watcher=$!
  trap 'kill "$watcher" 2>/dev/null || true' EXIT
fi
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY \
      no_proxy NO_PROXY
mkdir -p "$(dirname "$DEST")" 2>/dev/null
while :; do
  [ -f "$MARK" ] && [ "$(cat "$MARK" 2>/dev/null)" = "$REV" ] && exit 0
  sz=$(stat -f%z "$DEST" 2>/dev/null) || sz=0
  if [ "$SIZE" != "0" ] && [ "$sz" -gt "$SIZE" ]; then rm -f "$DEST"; sz=0; fi
  # resume guard: throttled xet CDN may ignore Range and make curl restart
  # (truncating hours of progress). Only resume when server truly says 206.
  # v3.6: xet range support is INTERMITTENT — probe several times to catch
  # a 206 window before discarding partial progress (2026-11-05 Jundot loop:
  # shards below half were wiped on nearly every reconnect, net-negative
  # bytes every minute — infinite restart loop).
  # v3.7: probe with GET, NOT HEAD — hf-mirror answers HEAD+Range with plain
  # 200 while GET+Range on the very same URL returns 206 (proved 2026-11-05
  # 13:20, Jundot shards stuck at 55% forever: worker's -I probe never saw
  # the 206 the download itself would get).
  if [ "$sz" -gt 0 ]; then
    code=$(curl --disable -fsL --noproxy '*' -r "${sz}-$((sz+1023))" -o /dev/null -w '%{http_code}' --max-time 20 "$URL" 2>/dev/null)
    tries=0
    while [ "$code" != "206" ] && [ "$tries" -lt 4 ]; do
      sleep 25
      code=$(curl --disable -fsL --noproxy '*' -r "${sz}-$((sz+1023))" -o /dev/null -w '%{http_code}' --max-time 20 "$URL" 2>/dev/null)
      tries=$((tries+1))
    done
    if [ "$code" != "206" ]; then
      sleep 60
      continue
    fi
  fi
  curl --disable -fL --noproxy '*' --retry 2 --retry-delay 2 --connect-timeout 20 \
       --max-time "$CT" --continue-at - -o "$DEST" "$URL" >/dev/null 2>&1
  rc=$?
  nsz=$(stat -f%z "$DEST" 2>/dev/null) || nsz=0
  if [ "$SIZE" != "0" ] && [ "$nsz" -ge "$SIZE" ]; then
    [ "$nsz" -gt "$SIZE" ] && { rm -f "$DEST"; continue; }
    printf '%s' "$REV" > "$MARK"
    exit 0
  fi
  if [ "$SIZE" = "0" ] && [ "$rc" -eq 0 ]; then
    printf '%s' "$REV" > "$MARK"
    exit 0
  fi
  sleep 1
done
