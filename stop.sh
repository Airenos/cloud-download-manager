#!/bin/sh
set -eu

cd "$(dirname "$0")"

stop_pid() {
  name="$1"
  pid_file="$2"
  if [ ! -s "$pid_file" ]; then
    echo "$name is not running"
    return
  fi
  pid="$(cat "$pid_file")"
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill "$pid"
    echo "stopped $name pid $pid"
  else
    echo "$name pid $pid is not running"
  fi
  rm -f "$pid_file"
}

stop_pid "Python server" data/server.pid
stop_pid "aria2c" data/aria2.pid
