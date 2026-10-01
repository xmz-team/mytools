#!/bin/bash -e
CFG_D="$HOME/.my/deepseek"
CFG_F="$CFG_D/config.cfg"
if [ -f "$CFG_F" ]; then
    source "$CFG_F"
    if [ -z "$API_KEY" ]; then
        read -p "You API: " api
        echo "API_KEY=$api" >> "$CFG_F"
        API_KEY="$api"
    fi
    if [ -z "$API_URL" ]; then
        read -p "You API URL: " url
        echo "API_URL=$url" >> "$CFG_F"
        API_URL="$url"
    fi
else
    mkdir -p "$CFG_D"
    read -p "You API: " api
    read -p "You API URL: " url
    {
        echo "API_KEY=$api"
        echo "API_URL=$url"
    } >> "$CFG_F"
    API_KEY="$api"
    API_URL="$url"
fi
export DEEPSEEK_API_KEY="$API_KEY"
mypath="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${mypath}/_myai" ]; then
    exec "$mypath/_myai" --base-url "$API_URL"
elif [ -f "${mypath}/_myai.py" ]; then
    exec "$mypath/_myai.py" --base-url "$API_URL"
else
    echo "[Error]: _myai not exist!" >&2
    exit 1
fi
