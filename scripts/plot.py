#!/usr/bin/env python3
"""Gráficos dos CSVs da coleta: por protocolo, uma execução com falha administrativa e uma com falha silenciosa."""
import csv
import json
import os
from pathlib import Path
import statistics
import tempfile
os.environ.setdefault('MPLCONFIGDIR', tempfile.mkdtemp(prefix='redes-mpl-'))
os.environ.setdefault('XDG_CACHE_HOME', tempfile.mkdtemp(prefix='redes-font-cache-'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import ticker
from matplotlib.patches import Patch
import scienceplots  # noqa: F401  registra os estilos 'science'

plt.style.use(['science', 'no-latex', 'muted'])

ROOT = Path(__file__).resolve().parents[1]
ORDER = ['bgp', 'ospf', 'rip']
COLORS = plt.rcParams['axes.prop_cycle'].by_key()['color']
plt.rcParams.update({'hatch.color': 'white', 'hatch.linewidth': 1})
# Mesma cor para cada protocolo em todos os gráficos; a segunda barra de um grupo vem hachurada.
PCOLOR = dict(zip(ORDER, COLORS))
LABELS = {'stable': 'Estabilidade', 'failure': 'Falha', 'admin': 'Falha administrativa', 'silent': 'Falha silenciosa'}
OUT = ROOT / 'results/graphs'
OUT.mkdir(parents=True, exist_ok=True)


def read(name, failure_types=('admin', 'silent')):
    with (ROOT / 'results/csv' / name).open() as file:
        rows = list(csv.DictReader(file))
    for protocol in ORDER:
        runs = {(r['failure_type'], r['run']) for r in rows if r['protocol'] == protocol}
        if runs != {(t, '1') for t in failure_types}:
            raise ValueError(f'{name}: esperada uma execução {"/".join(failure_types)} de {protocol}; encontradas {runs}')
    return rows


def admin(rows):
    return [r for r in rows if r['failure_type'] == 'admin']


def style(ax, bars=True):
    # Vírgula decimal (pt-BR); no eixo log também troca 10^n por 0,01 … 100.
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f'{v:g}'.replace('.', ',')))
    if bars: ax.tick_params(axis='x', which='both', top=False, bottom=False)


def label(ax, bars):
    ax.bar_label(bars, labels=[f'{b.get_height():.3g}'.replace('.', ',') for b in bars], padding=2, fontsize=8)


def graph(rows, metric, unit, title, filename, groups=None, field=None, log=False, samples=1):
    fig, ax = plt.subplots(figsize=(8, 4.5), layout='constrained')
    groups = groups or ['Todos']
    width = .8 / len(groups)
    for offset, group in enumerate(groups):
        means, lows, highs = [], [], []
        for protocol in ORDER:
            values = [float(r[metric]) for r in rows if r['protocol'] == protocol and (field is None or r[field] == group)]
            if len(values) != samples:
                raise ValueError(f'{filename}/{protocol}/{group}: esperadas {samples} amostras, recebidas {len(values)}')
            mean = statistics.mean(values)
            means.append(mean); lows.append(mean - min(values)); highs.append(max(values) - mean)
        positions = [i + (offset-(len(groups)-1)/2)*width for i in range(3)]
        bars = ax.bar(positions, means, width, yerr=[lows, highs] if samples > 1 else None, capsize=4,
                      color=[PCOLOR[p] for p in ORDER], hatch='///' if offset else None)
        label(ax, bars)
    ax.set_xticks(range(3), [p.upper() for p in ORDER])
    note = f'média de {samples} amostras; barras de erro: mínimo–máximo' if samples > 1 else 'execução única'
    ax.set_ylabel(unit); ax.set_title(f'{title}\n{note}', loc='left')
    # symlog: valores de 0 (sem perda) a centenas de segundos; linear abaixo da resolução de 10 ms.
    if log: ax.set_yscale('symlog', linthresh=.01)
    ax.set_ylim(0, ax.get_ylim()[1] * (3 if log else 1.08)); style(ax)  # folga para os rótulos
    if field:
        handles = [Patch(facecolor='gray', hatch='///' if i else None, label=LABELS[g]) for i, g in enumerate(groups)]
        ax.legend(handles=handles, loc='lower right', bbox_to_anchor=(1, 1), ncols=len(groups))
    fig.savefig(OUT / (filename + '.png'), dpi=200)
    plt.close(fig)

control = admin(read('control.csv'))
graph(control,'bits_per_s','bit/s','Taxa de controle nos cinco enlaces','taxa',['stable','failure'],'phase')
summary = read('summary.csv')
# RTT e tabela de rotas vêm da fase estável; basta a execução com falha administrativa.
graph(admin(read('routes.csv')),'rib_prefixes','Prefixos IPv4','Tamanho da tabela de rotas (5 roteadores)','tabela',samples=5)
graph(summary,'outage_s','s (escala log)','Tempo sem conexão H1–H4 (ping a cada 10 ms)','interrupcao',['admin','silent'],'failure_type',log=True)
graph(summary,'fib_change_s','s (escala log)','Tempo até a rota nova no kernel (maior valor entre R1 e R5)','fib',['admin','silent'],'failure_type',log=True)
timeline = admin(read('timeline.csv'))
# Um painel por protocolo, mesma escala: as rajadas não se sobrepõem.
fig, axes = plt.subplots(3, 1, figsize=(8, 6), sharex=True, sharey=True, layout='constrained')
for ax, protocol in zip(axes, ORDER):
    rows = [r for r in timeline if r['protocol'] == protocol]
    x, y = [int(r['second']) for r in rows], [int(r['packets']) for r in rows]
    ax.fill_between(x, y, step='post', color=PCOLOR[protocol], alpha=.3)
    ax.plot(x, y, drawstyle='steps-post', color=PCOLOR[protocol])
    ax.axvline(0, color='gray', linestyle='--', linewidth=1)
    ax.set_title(protocol.upper(), loc='left', fontweight='bold'); ax.set_ylim(bottom=0); style(ax, bars=False)
axes[0].text(0.3, axes[0].get_ylim()[1] * .85, 'troca de rota', color='gray', fontsize=8)
axes[1].set_ylabel('Pacotes de controle / s (5 enlaces)')
axes[-1].set_xlabel('Segundos em relação à troca de rota')
fig.suptitle('Tráfego de controle em torno da troca de rota (falha administrativa, execução única)', x=0.01, ha='left')
fig.savefig(OUT / 'linha-do-tempo.png', dpi=200)
plt.close(fig)

messages = read('mensagens.csv', ['admin'])
kinds = list(dict.fromkeys(r['message_type'] for r in messages))
colors = dict(zip(kinds, COLORS))
fig, ax = plt.subplots(figsize=(9, 4.5), layout='constrained')
labels = []
for i, (protocol, phase) in enumerate((p, f) for p in ORDER for f in ['stable', 'failure']):
    labels.append(f"{protocol.upper()}\n{'estável' if phase == 'stable' else 'falha'}")
    bottom = 0
    for kind in kinds:
        mean = sum(int(r['packets']) for r in messages if (r['protocol'], r['phase'], r['message_type']) == (protocol, phase, kind))
        if mean:
            ax.bar(i + i // 2 * .5, mean, .8, bottom=bottom, color=colors[kind], label=kind if kind not in ax.get_legend_handles_labels()[1] else None)
            bottom += mean
ax.set_xticks([i + i // 2 * .5 for i in range(6)], labels)
ax.set_ylabel('Mensagens / janela (5 enlaces)')
ax.set_title('Mensagens de controle por tipo (60 s estáveis; 60 s de falha)\nfalha administrativa, execução única de cada protocolo', loc='left')
style(ax); ax.legend(loc='upper right', ncols=2)
fig.savefig(OUT / 'mensagens.png', dpi=200)
plt.close(fig)

# Sem failure_type/run: lido direto, fora de read().
selection = ROOT / 'results/csv/selecao.csv'
if selection.exists():
    with selection.open() as file:
        rows = {r['protocol']: r for r in csv.DictReader(file)}
    fig, ax = plt.subplots(figsize=(8, 4.5), layout='constrained')
    bars = ax.bar(range(3), [float(rows[p]['rtt_mean_ms']) for p in ORDER], .6, color=[PCOLOR[p] for p in ORDER])
    ax.bar_label(bars, labels=[f'{b.get_height():.2f} ms'.replace('.', ',') for b in bars], padding=3)  # o OSPF fica abaixo de 1 ms e a barra some
    ax.set_xticks(range(3), [f"{p.upper()}\n{rows[p]['path']}" for p in ORDER])
    ax.set_ylabel('RTT médio H1–H4 (ms)')
    ax.set_title('Seleção de rotas com R1–R5 lento (+30 ms por sentido)\n20 pings, execução única; caminho do traceroute sob cada barra', loc='left')
    ax.set_ylim(bottom=0); style(ax)
    fig.savefig(OUT / 'selecao.png', dpi=200)
    plt.close(fig)
else:
    print(f'{selection} ausente: selecao.png não gerado (rode collect.sh)')

def replies(folder):
    # Segundos, desde o início da falha, de cada resposta do ping de 10 ms (recovery-ping.log: monotônico + linha do ping).
    start = next(e['monotonic'] for e in json.loads((folder / 'events.json').read_text()) if e['name'] == 'failure_start')
    return [float(line.split()[0]) - start for line in (folder / 'recovery-ping.log').read_text().splitlines() if 'bytes from' in line]  # só respostas; 'Destination Unreachable' também traz icmp_seq


# Uma faixa por protocolo com um traço por resposta: o buraco é o tempo sem conexão. PCAPs e logs ficam em results/raw/.
raw = ROOT / 'results/raw'
runs = {(p, t): sorted(raw.glob(f'{p}-{t}-1-*'))[-1:] for p in ORDER for t in ('admin', 'silent')}
if all(runs.values()):
    outages = {(r['protocol'], r['failure_type']): float(r['outage_s']) for r in summary}
    fig, axes = plt.subplots(2, 1, figsize=(8, 5.5), layout='constrained')
    for ax, kind, xmax in zip(axes, ('admin', 'silent'), (30, 200)):
        for row, protocol in enumerate(ORDER):
            ax.eventplot(replies(runs[protocol, kind][0]), lineoffsets=row, linelengths=.7, linewidths=.3, colors=PCOLOR[protocol])
            ax.text(xmax, row, f"  {outages[protocol, kind]:.3g} s sem resposta".replace('.', ','), va='center', fontsize=8)
        ax.axvline(0, color='gray', linestyle='--', linewidth=1)
        ax.set_xlim(-5, xmax); ax.set_yticks(range(3), [p.upper() for p in ORDER]); ax.invert_yaxis()
        ax.tick_params(axis='y', which='both', left=False, right=False)
        ax.set_title(LABELS[kind], loc='left', fontweight='bold')
    axes[-1].set_xlabel('Segundos desde a falha em R1–R5 (cada traço é um ping respondido; buraco = sem conexão)')
    fig.suptitle('Pings H1→H4 a cada 10 ms durante a falha (execução única)', x=0.01, ha='left')
    fig.savefig(OUT / 'pings.png', dpi=200)
    plt.close(fig)
else:
    print(f'{raw} sem as execuções de falha: pings.png não gerado (rode collect.sh)')
print(f'Gráficos gerados em {OUT}')
