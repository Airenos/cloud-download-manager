#!/bin/sh
set -eu

cd "$(dirname "$0")"
mkdir -p downloads logs data

python3 -c 'import download_server; removed = download_server.cleanup_expired(); print("manual cleanup removed:", ",".join(removed) if removed else "none")' >> logs/cleanup.log 2>&1
