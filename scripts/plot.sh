#!/bin/bash
# Gera os gráficos num contêiner descartável com o Matplotlib fixado em requirements.txt; sem venv no host.
set -euo pipefail
cd "$(dirname "$0")/.."
exec docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD":/w -w /w python:3.13-slim \
  sh -c 'pip install -q --user --disable-pip-version-check --no-warn-script-location -r requirements.txt && python scripts/plot.py'
