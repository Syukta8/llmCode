#!/usr/bin/env bash
set -euo pipefail
BASE_URL="${BASE_URL:-http://127.0.0.1:8080/v1}"
MODEL="${1:-4b}"; case "$MODEL" in 4b|9b) ;; *) echo 'Usage: 05-validate-api.sh [4b|9b]' >&2; exit 2;; esac
ID="hauhaucs-qwen35-$MODEL"
echo '=== Health ==='; curl -fsS "${BASE_URL%/v1}/health"; echo
echo '=== Models ==='; curl -fsS "$BASE_URL/models" | jq '.data[].id'
echo '=== Basic chat ==='
curl -fsS "$BASE_URL/chat/completions" -H 'Content-Type: application/json' -d "$(jq -nc --arg id "$ID" '{model:$id,messages:[{role:"user",content:"Write a one-line Python hello world program."}],max_tokens:128,temperature:0}')" | jq '{content:.choices[0].message.content,usage:.usage}'
echo '=== Required tool call ==='
RESULT=$(curl -fsS "$BASE_URL/chat/completions" -H 'Content-Type: application/json' -d "$(jq -nc --arg id "$ID" '{model:$id,messages:[{role:"user",content:"Call calculate_sum with 17 and 25."}],tools:[{type:"function",function:{name:"calculate_sum",description:"Add two numbers",parameters:{type:"object",properties:{a:{type:"integer"},b:{type:"integer"}},required:["a","b"]}}}],tool_choice:"required",max_tokens:512}')")
echo "$RESULT" | jq '{tool_calls:.choices[0].message.tool_calls,content:.choices[0].message.content}'
if echo "$RESULT" | jq -e '.choices[0].message.tool_calls | length > 0' >/dev/null; then echo 'PASS: Tool call emitted'; else echo 'FAIL: No structured tool call' >&2; exit 3; fi
