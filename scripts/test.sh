#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
[ "$#" -eq 1 ] || { echo 'Uso: test.sh bgp|ospf|rip' >&2; exit 2; }
exec python3 scripts/experiment.py test "$1"
