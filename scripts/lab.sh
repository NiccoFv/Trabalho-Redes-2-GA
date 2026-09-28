#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PROTOCOL=bgp
case "${1:-}" in
  up)
    case "${2:-}" in bgp|ospf|rip) export PROTOCOL="$2";; *) echo 'Uso: lab.sh up bgp|ospf|rip' >&2; exit 2;; esac
    [ "$#" -eq 2 ] || exit 2
    mkdir -p results/raw
    docker compose down --remove-orphans
    docker compose up -d --build
    ;;
  down|status)
    [ "$#" -eq 1 ] || exit 2
    if [ "$1" = down ]; then docker compose down; else docker compose ps; fi
    ;;
  shell)
    [ "$#" -eq 2 ] || exit 2
    case "$2" in r[1-5]) docker compose exec "$2" vtysh;; *) exit 2;; esac
    ;;
  *) echo 'Uso: lab.sh up bgp|ospf|rip | down | status | shell r1..r5' >&2; exit 2;;
esac
