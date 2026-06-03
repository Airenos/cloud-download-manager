#!/bin/sh
set -eu

cd "$(dirname "$0")"

mkdir -p downloads logs data

if ! command -v aria2c >/dev/null 2>&1; then
  echo "aria2c not found. Install it first:"
  echo "  Debian/Ubuntu: sudo apt update && sudo apt install -y aria2"
  echo "  Alpine:        sudo apk add aria2"
  echo "  CentOS/RHEL:   sudo yum install -y aria2"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install Python 3 first."
  exit 1
fi

make_secret() {
  python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
}

if [ ! -s data/admin_password.txt ]; then
  make_secret > data/admin_password.txt
fi
chmod 600 data/admin_password.txt

if [ ! -s data/aria2_rpc_secret.txt ]; then
  make_secret > data/aria2_rpc_secret.txt
fi
chmod 600 data/aria2_rpc_secret.txt

ARIA2_SECRET="$(cat data/aria2_rpc_secret.txt)"
cat > data/aria2.conf <<EOF
enable-rpc=true
rpc-listen-all=false
rpc-listen-address=127.0.0.1
rpc-listen-port=6800
rpc-secret=${ARIA2_SECRET}
dir=$(pwd)/downloads
continue=true
file-allocation=none
max-concurrent-downloads=2
max-connection-per-server=4
split=4
auto-file-renaming=true
allow-overwrite=false
save-session=$(pwd)/data/aria2.session
input-file=$(pwd)/data/aria2.session
save-session-interval=60
EOF
chmod 600 data/aria2.conf
touch data/aria2.session
chmod 600 data/aria2.session

is_running() {
  pid_file="$1"
  [ -s "$pid_file" ] && kill -0 "$(cat "$pid_file")" >/dev/null 2>&1
}

if is_running data/aria2.pid; then
  echo "aria2c already running with pid $(cat data/aria2.pid)"
else
  aria2c --conf-path=data/aria2.conf >> logs/aria2.log 2>&1 &
  echo "$!" > data/aria2.pid
  echo "started aria2c pid $(cat data/aria2.pid)"
fi

if is_running data/server.pid; then
  echo "Python server already running with pid $(cat data/server.pid)"
else
  HOST="${HOST:-0.0.0.0}" PORT="${PORT:-8081}" python3 download_server.py >> logs/server.log 2>&1 &
  echo "$!" > data/server.pid
  echo "started Python server pid $(cat data/server.pid)"
fi

echo "Temporary download site: http://127.0.0.1:${PORT:-8081}/"
echo "Admin password: cat data/admin_password.txt"
