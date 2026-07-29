#!/bin/sh
set -eu

gateway_url="${GATEWAY_URL:-http://127.0.0.1:8000}"
gateway_key="${GATEWAY_API_KEY:?GATEWAY_API_KEY is required}"

curl --fail --silent --show-error "$gateway_url/v1/models" \
  -H "Authorization: Bearer $gateway_key" >/dev/null

curl --fail --silent --show-error "$gateway_url/v1/chat/completions" \
  -H "Authorization: Bearer $gateway_key" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5-web","messages":[{"role":"user","content":"hello"}]}'

curl --fail --silent --show-error --no-buffer "$gateway_url/v1/chat/completions" \
  -H "Authorization: Bearer $gateway_key" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5-web","messages":[{"role":"user","content":"hello"}],"stream":true}'

