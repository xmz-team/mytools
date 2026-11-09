#!/bin/bash -e
# myai.sh
# Created by XMZ <xmz-team@outlook.com>
# Copyright (c) 2026 XMZ <xmz-team@outlook.com>
# SPDX-License-Identifier: MIT

CFG_D="$HOME/.my/deepseek"
CFG_F="$CFG_D/config.cfg"
mkdir -p "$CFG_D"
chmod 700 "$CFG_D"

if [ -f "$CFG_F" ]; then
    chmod 600 "$CFG_F" 2>/dev/null || true
    source "$CFG_F"
fi

if [ -z "$API_KEY" ]; then
    read -p "API Key: " API_KEY
    echo "API_KEY=$API_KEY" >> "$CFG_F"
fi

if [ -z "$API_URL" ]; then
    read -p "Base URL [https://api.deepseek.com]: " API_URL
    API_URL="${API_URL:-https://api.deepseek.com}"
    echo "API_URL=$API_URL" >> "$CFG_F"
fi

if [ -z "$MODEL_NAME" ]; then
    read -p "Model name [deepseek-flash]: " MODEL_NAME
    MODEL_NAME="${MODEL_NAME:-deepseek-flash}"
    echo "MODEL_NAME=$MODEL_NAME" >> "$CFG_F"
fi

if [ -z "$AI_PASSWORD" ]; then
    read -rsp "Your password: " AI_PASSWORD
    echo
    printf 'AI_PASSWORD=%q\n' "$AI_PASSWORD" >> "$CFG_F"
fi

export DEEPSEEK_API_KEY="$API_KEY"
export DEEPSEEK_BASE_URL="$API_URL"
export DEEPSEEK_MODEL="$MODEL_NAME"
export AI_PASSWORD="$AI_PASSWORD"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$DIR/_myai" ]; then
    exec "$DIR/_myai" "$@"
elif [ -f "$DIR/_myai.py" ]; then
    exec python3 "$DIR/_myai.py" "$@"
else
    echo "[error] _myai not found" >&2
    exit 1
fi
