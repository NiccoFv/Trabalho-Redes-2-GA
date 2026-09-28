#!/bin/bash
# Coleta completa: por protocolo, uma execução com falha administrativa em R1–R5 (60 s), uma com falha silenciosa (240 s)
# e o experimento de seleção de rotas; depois mensagens por tipo e gráficos. Recomeça do zero a cada chamada.
set -euo pipefail
cd "$(dirname "$0")/.."
[ "$#" -eq 0 ] || { echo 'Uso: collect.sh' >&2; exit 2; }
rm -rf results/csv results/graphs
mkdir -p results/raw
exec > >(tee results/raw/collect.log) 2>&1  # mensagens lê daqui as pastas desta rodada
trap './scripts/lab.sh down' EXIT
for protocol in bgp ospf rip; do
  ./scripts/lab.sh up "$protocol"
  python3 scripts/experiment.py collect "$protocol"
  ./scripts/lab.sh up "$protocol"
  python3 scripts/experiment.py collect "$protocol" --failure-type silent --failure 240
  ./scripts/lab.sh up "$protocol"  # recria o cenário: a seleção parte de um anel limpo
  python3 scripts/experiment.py selecao "$protocol"
done
python3 scripts/experiment.py mensagens
./scripts/plot.sh
