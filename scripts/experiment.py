#!/usr/bin/env python3
"""Validação e coleta do anel FRR. Somente biblioteca padrão."""
import argparse
import csv
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess as sp
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
COMPOSE = ['docker', 'compose']
ROUTERS = [f'r{i}' for i in range(1, 6)]
PEERS = {'r1': ['10.0.12.2', '10.0.51.1'], 'r2': ['10.0.12.1', '10.0.23.2'],
         'r3': ['10.0.23.1', '10.0.34.2'], 'r4': ['10.0.34.1', '10.0.45.2'],
         'r5': ['10.0.45.1', '10.0.51.2']}
FILTERS = {'bgp': 'tcp port 179', 'ospf': 'ip proto 89', 'rip': 'udp port 520'}
DISPLAY = {'bgp': 'tcp.port == 179', 'ospf': 'ospf', 'rip': 'udp.port == 520'}
# Mensagens de atualização; RIP não diferencia atualização periódica de disparada.
UPDATES = {'bgp': 'bgp.type == 2 || bgp.type == 3', 'ospf': 'ospf.msg != 1', 'rip': 'rip.command == 2'}
FIELDS = {'bgp': 'bgp.type', 'ospf': 'ospf.msg', 'rip': 'rip.command'}
MESSAGES = {'bgp': {'1': 'OPEN', '2': 'UPDATE', '3': 'NOTIFICATION', '4': 'KEEPALIVE'},
            'ospf': {'1': 'Hello', '2': 'DB Description', '3': 'LS Request', '4': 'LS Update', '5': 'LS Ack'},
            'rip': {'1': 'Request', '2': 'Response'}}
# Prefixo remoto e próximo salto alternativo observados pelo monitor de cada ponta de R1–R5.
MONITOR = {'r1': ('192.168.4.0/24', '10.0.12.2'), 'r5': ('192.168.1.0/24', '10.0.45.1')}
PROBE_INTERVAL = 0.01
# Espera depois de o cenário ficar pronto: com as adjacências recém-formadas, o OSPF ainda inunda uma rajada
# de LS Update que cairia na fase estável.
WARMUP = 30
TIMEOUT = 300


def run(args, check=True, timeout=30, stdin=None):
    return sp.run(args, text=True, stdin=stdin, stdout=sp.PIPE, stderr=sp.PIPE,
                  check=check, timeout=timeout).stdout


def execute(router, *args, check=True):
    return run(COMPOSE + ['exec', '-T', router] + list(args), check=check)


def vty(router, command):
    return execute(router, 'vtysh', '-c', command)


def interface(router, address):
    for item in json.loads(execute(router, 'ip', '-j', '-4', 'addr')):
        if any(a['local'] == address for a in item['addr_info']):
            return item['ifname']
    raise RuntimeError(f'{router}: interface com {address} ausente')


def link(state, events=None, failure_type='admin', check=True):
    # admin: ip link down, detectado localmente; silent: enlace up, netem descarta tudo e só os timers detectam.
    for router, address in [('r1', '10.0.51.2'), ('r5', '10.0.51.1')]:
        name = interface(router, address)
        if failure_type == 'admin': command = ['ip', 'link', 'set', name, state]
        elif state == 'down': command = ['tc', 'qdisc', 'add', 'dev', name, 'root', 'netem', 'loss', '100%']
        else: command = ['tc', 'qdisc', 'del', 'dev', name, 'root']
        if events: events(f'{router}_{state}_start')
        execute(router, *command, check=check)
        if events: events(f'{router}_{state}_end')


def fib(alternate=False):
    # Conferir todo o caminho, não somente a saída de R1.
    hops = [('r1', '10.0.12.2'), ('r2', '10.0.23.2'), ('r3', '10.0.34.2')] if alternate else [('r1', '10.0.51.1'), ('r5', '10.0.45.1')]
    for router, gateway in hops:
        route = json.loads(execute(router, 'ip', '-j', 'route', 'get', '192.168.4.10'))[0]
        if route.get('gateway') != gateway:
            raise RuntimeError(f'{router}: próximo salto {route.get("gateway")}, esperado {gateway}')


def ready(protocol):
    active = run(COMPOSE + ['ps', '--status', 'running', '--services']).split()
    if set(active) != set(ROUTERS + [f'h{i}' for i in range(1, 6)]):
        raise RuntimeError('Os dez contêineres devem estar ativos')
    for i, router in enumerate(ROUTERS, 1):
        config = vty(router, 'show running-config')
        if f'router {protocol}' not in config or any(f'router {other}' in config for other in FILTERS if other != protocol):
            raise RuntimeError(f'{router}: cenário incorreto')
        if json.loads(execute(router, 'ip', '-j', 'route', 'show', 'default')):
            raise RuntimeError(f'{router}: rota padrão inesperada')
        routes = json.loads(vty(router, 'show ip route json'))
        for j in range(1, 6):
            if j == i: continue
            entries = routes.get(f'192.168.{j}.0/24', [])
            if not any(r.get('protocol') == protocol and r.get('selected') and r.get('installed') for r in entries):
                raise RuntimeError(f'{router}: LAN {j} não instalada por {protocol}')
        if protocol == 'bgp':
            peers = json.loads(vty(router, 'show bgp ipv4 unicast summary json'))['peers']
            if set(peers) != set(PEERS[router]) or any(p.get('state') != 'Established' or not isinstance(p.get('pfxRcd'), int) or p['pfxRcd'] == 0 for p in peers.values()):
                raise RuntimeError(f'{router}: sessões BGP incompletas ou sem prefixos')
        elif protocol == 'ospf':
            neighbors = json.loads(vty(router, 'show ip ospf neighbor json'))['neighbors']
            rows = [entry for values in neighbors.values() for entry in values]
            if len(rows) != 2 or any(not r['nbrState'].startswith('Full') for r in rows):
                raise RuntimeError(f'{router}: vizinhos OSPF incompletos')
        else:
            status = vty(router, 'show ip rip status')
            if not all(re.search(r'\b' + re.escape(peer) + r'\s', status) for peer in PEERS[router]):
                raise RuntimeError(f'{router}: atualizações RIP dos dois pares ausentes')
    for j in range(2, 6):
        execute('h1', 'ping', '-n', '-c', '1', '-W', '1', f'192.168.{j}.10')


def wait_for(check, timeout=TIMEOUT):
    deadline = time.monotonic() + timeout
    last = ''
    while time.monotonic() < deadline:
        try:
            check()
            return
        except (RuntimeError, sp.SubprocessError, ValueError, KeyError) as exc:
            last = str(exc)
            time.sleep(1)
    raise RuntimeError(f'Timeout ({timeout}s): {last}')


def path_check(alternate=False):
    fib(alternate)
    execute('h1', 'ping', '-n', '-c', '1', '-W', '1', '192.168.4.10')


def trace(folder, name):
    output = execute('h1', 'traceroute', '-I', '-n', '-q', '1', '-w', '1', '192.168.4.10')
    (folder / f'{name}-traceroute.txt').write_text(output)
    print(output, flush=True)


def route_path(output):
    # Roteador de cada salto: 192.168.N.1 é RN; em 10.0.XY.Z, Z=1 é RX e Z=2 é RY. O host final e os '*' ficam fora.
    path = []
    for net, host in re.findall(r'^\s*\d+\s+\d+\.\d+\.(\d+)\.(\d+)', output, re.M):
        if len(net) == 2: path.append('R' + net[int(host) - 1])
        elif host == '1': path.append('R' + net)
    return '>'.join(path)


def diagnostics(folder):
    folder.mkdir(parents=True, exist_ok=True)
    for router in ROUTERS:
        for label, command in [('routes', 'show ip route json'), ('config', 'show running-config'), ('bgp', 'show bgp summary'), ('bgp-timers', 'show bgp neighbors'), ('ospf-timers', 'show ip ospf interface'), ('ospf', 'show ip ospf neighbor'), ('rip', 'show ip rip status')]:
            try: (folder / f'{router}-{label}.txt').write_text(vty(router, command))
            except sp.SubprocessError: pass
    (folder / 'compose.log').write_text(run(COMPOSE + ['logs', '--no-color'], check=False))


def validate(protocol):
    folder = ROOT / 'results/raw' / f'test-{protocol}'
    folder.mkdir(parents=True, exist_ok=True)
    try:
        wait_for(lambda: ready(protocol))
        wait_for(path_check)
        trace(folder, 'normal')
        link('down')
        wait_for(lambda: path_check(True))
        trace(folder, 'alternate')
        link('up')
        wait_for(lambda: ready(protocol))
        wait_for(path_check)
        trace(folder, 'restored')
        diagnostics(folder)
        print(f'PASS: {protocol}: vizinhos, LANs, ping, FIB, falha e restauração', flush=True)
    except BaseException:
        diagnostics(folder)
        raise
    finally:
        link('up')
        link('up', failure_type='silent', check=False)


def selection(protocol):
    # R1–R5 fica lento (30 ms por sentido). Só o OSPF enxerga o atraso, via custo 100 configurado à mão;
    # RIP conta saltos e BGP compara AS_PATH, então os dois continuam pelo enlace lento.
    folder = ROOT / 'results/raw' / f'selecao-{protocol}'
    folder.mkdir(parents=True, exist_ok=True)
    try:
        wait_for(lambda: ready(protocol))
        wait_for(path_check)
        for router, address in [('r1', '10.0.51.2'), ('r5', '10.0.51.1')]:
            name = interface(router, address)
            execute(router, 'tc', 'qdisc', 'replace', 'dev', name, 'root', 'netem', 'delay', '30ms')
            # Só na configuração em execução: os frr.conf são montados somente leitura e nada é gravado.
            if protocol == 'ospf': execute(router, 'vtysh', '-c', 'conf t', '-c', f'interface {name}', '-c', 'ip ospf cost 100')
        if protocol == 'ospf': wait_for(lambda: fib(True))
        else:
            time.sleep(10)
            fib(False)
        trace(folder, 'selecao')
        output = execute('h1', 'ping', '-n', '-c', '20', '-i', '0.2', '-W', '1', '192.168.4.10', check=False)
        (folder / 'selecao-ping.log').write_text(output)
        metrics = ping_metrics(output)
        gateway = json.loads(execute('r1', 'ip', '-j', 'route', 'get', '192.168.4.10'))[0].get('gateway')
        diagnostics(folder)
        append_csv('selecao.csv', {'protocol': protocol, 'next_hop_r1': gateway,
                                   'path': route_path((folder / 'selecao-traceroute.txt').read_text()),
                                   **{key: metrics[key] for key in ['rtt_mean_ms', 'rtt_p95_ms', 'loss_pct']}})
    except BaseException:
        diagnostics(folder)
        raise
    finally:
        # O custo OSPF alterado some no próximo lab.sh up, que recria os contêineres.
        link('up', failure_type='silent', check=False)


def percentile(values, q):
    values = sorted(values)
    position = (len(values) - 1) * q
    lo = math.floor(position)
    return values[lo] + (values[math.ceil(position)] - values[lo]) * (position - lo)


def ping_metrics(output):
    rtts = [float(v) for v in re.findall(r'time[=<]([\d.]+) ms', output)]
    match = re.search(r'(\d+) packets transmitted, (\d+) received', output)
    if not match or not rtts: raise RuntimeError('Ping sem amostras válidas')
    sent, received = map(int, match.groups())
    return {'rtt_mean_ms': statistics.mean(rtts), 'rtt_median_ms': statistics.median(rtts),
            'rtt_p95_ms': percentile(rtts, .95), 'loss_pct': 100 * (sent - received) / sent}


def outage(responses):
    # icmp_seq perdidos entre a primeira e a última resposta × intervalo real médio de envio: o ping
    # nominal de 10 ms atrasa com o escalonamento. Respostas DUP contam uma vez.
    first, last = min(responses), max(responses)
    lost = last[0] - first[0] + 1 - len({seq for seq, _ in responses})
    return lost * (last[1] - first[1]) / (last[0] - first[0])


def monitor(log):
    # Linhas de `ip -ts monitor` com horário UTC (TZ=UTC no exec); continuações sem horário são ignoradas.
    for line in log.splitlines():
        match = re.match(r'\[(\d{4}-\d\d-\d\dT[\d:.]+)\] (?:\[\w+\])?(.*)', line)
        if match: yield dt.datetime.fromisoformat(match[1]).replace(tzinfo=dt.timezone.utc).timestamp(), match[2]


def link_down(log):
    return next(when for when, text in monitor(log) if 'ring51' in text and 'state DOWN' in text)


def fib_change(log, router, begin):
    # Segundos entre begin e a instalação da rota alternativa neste roteador.
    prefix, gateway = MONITOR[router]
    for when, text in monitor(log):
        if when >= begin and text.startswith(prefix + ' ') and f'via {gateway} ' in text:
            return when - begin
    raise RuntimeError(f'{router}: troca de rota para {prefix} via {gateway} ausente no monitor')


def spawn(router, pidfile, args, output):
    # PID gravado no contêiner: matar o docker exec não encerra o processo interno.
    command = COMPOSE + ['exec', '-T', '-e', 'TZ=UTC', router, 'sh', '-c', 'echo $$ > "$1"; shift; exec "$@"', 'spawn', pidfile] + args
    return sp.Popen(command, stdout=output, stderr=output)


def append_csv(filename, row):
    path = ROOT / 'results/csv' / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open('a', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=list(row))
        if not exists: writer.writeheader()
        writer.writerow(row)


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def collect(protocol, repetition, stable=60, failure=60, failure_type='admin'):
    folder = ROOT / 'results/raw' / f'{protocol}-{failure_type}-{repetition}-{int(time.time())}'
    folder.mkdir(parents=True)
    events = []
    captures = []
    stop = threading.Event()
    responses = []
    monitors = []
    probes = None
    def event(name):
        item = {'name': name, 'monotonic': time.monotonic(), 'utc': utc(), 'epoch': time.time()}
        events.append(item)
        if name == 'r1_down_start':
            events.append(dict(item, name='failure_start'))
        (folder / 'events.json').write_text(json.dumps(events, indent=2))
        return item
    def read_ping():
        with (folder / 'recovery-ping.log').open('w') as log:
            for line in probes.stdout:
                now = time.monotonic()
                log.write(f'{now:.9f} {line}'); log.flush()
                match = re.search(r'icmp_seq=(\d+).*time[=<]([\d.]+)', line)
                if match: responses.append((int(match[1]), now))
    def sample_stats():
        while not stop.is_set():
            try:
                output = run(COMPOSE + ['ps', '-q'] + ROUTERS).split()
                stats = run(['docker', 'stats', '--no-stream', '--format', '{{json .}}'] + output)
                sample_time = time.monotonic()
                with (folder / 'stats.jsonl').open('a') as log:
                    for line in stats.splitlines():
                        log.write(json.dumps({'monotonic': sample_time, 'stats': json.loads(line)}) + '\n')
            except sp.SubprocessError: pass
            stop.wait(2)
    try:
        wait_for(lambda: ready(protocol)); wait_for(path_check)
        time.sleep(WARMUP)
        diagnostics(folder)
        route_rows = []
        for router in ROUTERS:
            routes = json.loads(vty(router, 'show ip route json'))
            remote = sum(any(r.get('protocol') == protocol and r.get('selected') and r.get('installed') for r in entries)
                         for prefix, entries in routes.items() if prefix.startswith('192.168.') and prefix.endswith('/24') and prefix != f'192.168.{router[1:]}.0/24')
            route_rows.append({'protocol': protocol, 'failure_type': failure_type, 'run': repetition, 'router': router, 'rib_prefixes': len(routes), 'remote_lans': remote})
        for router, subnet, address in [('r1','12','10.0.12.1'), ('r2','23','10.0.23.1'), ('r3','34','10.0.34.1'), ('r4','45','10.0.45.1'), ('r5','51','10.0.51.1')]:
            name = interface(router, address)
            pcap = folder / f'link{subnet}.pcap'
            err = (folder / f'link{subnet}-tcpdump.log').open('w')
            process = spawn(router, f'/tmp/capture-{subnet}.pid', ['tcpdump','-U','-n','-i',name,'-w',f'/captures/{folder.name}/{pcap.name}', FILTERS[protocol]], err)
            captures.append((router, f'/tmp/capture-{subnet}.pid', process, err, pcap))
            wait_for(lambda: capture_ready(pcap, process), timeout=15)
            event(f'capture_{subnet}_ready')
        # Iniciados junto às capturas, bem antes da falha; o horário é do relógio do kernel compartilhado.
        for router in MONITOR:
            log = (folder / f'{router}-monitor.log').open('w')
            monitors.append((router, '/tmp/monitor.pid', spawn(router, '/tmp/monitor.pid', ['ip','-ts','monitor','link','route'], log), log))
        stats_thread = threading.Thread(target=sample_stats)
        stats_thread.start()
        start = event('stable_start')
        output = execute('h1','ping','-n','-c','100','-i','0.2','-W','1','192.168.4.10',check=False)
        (folder/'rtt-ping.log').write_text(output)
        metrics = ping_metrics(output)
        time.sleep(max(0, start['monotonic'] + stable - time.monotonic()))
        event('stable_end')
        probes = sp.Popen(COMPOSE + ['exec','-T','h1','ping','-n','-O','-i',str(PROBE_INTERVAL),'-W','1','192.168.4.10'], stdout=sp.PIPE, stderr=sp.STDOUT, text=True, bufsize=1)
        ping_thread = threading.Thread(target=read_ping); ping_thread.start()
        wait_for(lambda: require(len(responses) >= 5, 'aguardando cinco pings'), timeout=15)
        link('down', event, failure_type)
        start = next(e for e in events if e['name'] == 'failure_start')
        # Verificação de correção do caminho alternativo; o tempo vem do monitor do kernel.
        wait_for(lambda: path_check(True), timeout=min(TIMEOUT, failure))
        event('alternate_fib_confirmed')
        trace(folder, 'alternate')
        diagnostics(folder / 'failure')  # evidência da detecção, p. ex. "Hold Timer Expired" em show bgp neighbors
        time.sleep(max(0, start['monotonic'] + failure - time.monotonic()))
        event('failure_end')
        # Encerrar o ping antes da restauração: a volta ao caminho original não entra em outage_s.
        execute('h1','pkill','-INT','ping',check=False)
        probes.wait(timeout=10); ping_thread.join(timeout=10)
        link('up', event, failure_type)
        wait_for(lambda: ready(protocol)); wait_for(path_check)
        event('restored'); trace(folder, 'restored')
        metrics['outage_s'] = outage(responses)
    except BaseException:
        diagnostics(folder)
        raise
    finally:
        stop.set()
        if 'stats_thread' in locals(): stats_thread.join(timeout=35)
        try:
            if probes and probes.poll() is None:
                execute('h1','pkill','-INT','ping',check=False)
                probes.wait(timeout=10)
                ping_thread.join(timeout=10)
            for router, pidfile, process, log, *_ in captures + monitors:
                if process.poll() is None:
                    execute(router,'sh','-c',f'kill -INT "$(cat {pidfile})"',check=False)
                    process.wait(timeout=10)
                log.close()
        finally:
            link('up')
            link('up', failure_type='silent', check=False)
    # Troca de rota mais lenta entre as duas pontas. Início: primeiro link down nos logs (R5 pode trocar
    # a rota pelo LSA/UPDATE de R1 antes da própria queda) ou, na falha silenciosa, o comando tc.
    logs = {router: (folder/f'{router}-monitor.log').read_text() for router in MONITOR}
    begin = min(map(link_down, logs.values())) if failure_type == 'admin' else start['epoch']
    metrics['fib_change_s'] = max(fib_change(logs[router], router, begin) for router in MONITOR)
    for router in ['r1','r5']:
        begin = next(e['monotonic'] for e in events if e['name']==f'{router}_down_start')
        end = next(e['monotonic'] for e in events if e['name']==f'{router}_down_end')
        metrics[f'{router}_down_duration_s'] = end-begin
    # Pacotes de todos os enlaces.
    frames, updates = [], []
    for router, pidfile, process, err, pcap in captures:
        frames += tshark_frames(pcap, DISPLAY[protocol])
        updates += tshark_frames(pcap, f'({DISPLAY[protocol]}) && ({UPDATES[protocol]})')
    epoch = lambda name: next(e['epoch'] for e in events if e['name'] == name)
    change = epoch('failure_start') + metrics['fib_change_s']
    windows = [('stable', epoch('stable_start'), stable), ('failure', epoch('failure_start'), failure),
               ('convergence', epoch('failure_start') if failure_type == 'admin' else change - 1, 10)]
    for phase, begin, seconds in windows:
        inside = [size for t, size in frames if begin <= t < begin + seconds]
        append_csv('control.csv', {'protocol':protocol,'failure_type':failure_type,'run':repetition,'phase':phase,'duration_s':seconds,
                                   'packets':len(inside),'update_packets':sum(begin <= t < begin + seconds for t, _ in updates),'bits_per_s':sum(inside)*8/seconds})
    # Até +30 s: a janela de falha tem 60 s e a restauração não entra na linha do tempo.
    for second in range(-10, 30):
        begin = change + second
        append_csv('timeline.csv', {'protocol':protocol,'failure_type':failure_type,'run':repetition,'second':second,
                                    'packets':sum(begin <= t < begin + 1 for t, _ in frames),'update_packets':sum(begin <= t < begin + 1 for t, _ in updates)})
    consolidate_stats(folder, events, protocol, repetition, failure_type)
    for row in route_rows: append_csv('routes.csv', row)
    append_csv('summary.csv', {'protocol':protocol,'failure_type':failure_type,'run':repetition,**metrics})
    print(f'Coleta concluída: {folder}',flush=True)


def tshark_frames(pcap, display):
    # Leitura por stdin: o perfil AppArmor do tshark no Ubuntu só o deixa abrir arquivos em /tmp.
    with pcap.open('rb') as file:
        output = run(['tshark','-r','-','-Y',display,'-T','fields','-e','frame.time_epoch','-e','frame.len'], timeout=120, stdin=file)
    return [(float(t), int(size)) for t, size in (line.split() for line in output.splitlines())]


def message_types(protocol, field):
    # Um segmento TCP pode trazer várias mensagens BGP ("2,4"): conta cada uma. TCP/179 sem bgp.type é ACK puro.
    if not field: return ['TCP ACK']
    return [MESSAGES[protocol].get(code, code) for code in field.split(',')]


def messages(log=ROOT / 'results/raw/collect.log'):
    # Mensagens por tipo nas execuções com falha administrativa da última coleta, nas janelas de control.csv.
    folders = sorted({Path(p).name for p in re.findall(r'^Coleta concluída: (.+)$', log.read_text(), re.M) if '-admin-' in p})
    require(folders, f'{log}: nenhuma execução com falha administrativa')
    rows = []
    for name in folders:
        protocol, failure_type, repetition, _ = name.split('-')
        folder = ROOT / 'results/raw' / name
        events = {e['name']: e['epoch'] for e in json.loads((folder / 'events.json').read_text())}
        windows = [(phase, events[f'{phase}_start'], events[f'{phase}_end']) for phase in ['stable', 'failure']]
        counts = {}
        for pcap in sorted(folder.glob('link??.pcap')):
            with pcap.open('rb') as file:
                output = run(['tshark', '-r', '-', '-Y', DISPLAY[protocol], '-T', 'fields', '-E', 'separator=/t',
                              '-e', 'frame.time_epoch', '-e', FIELDS[protocol]], timeout=120, stdin=file)
            for line in output.splitlines():
                when, _, field = line.partition('\t')
                for phase, begin, end in windows:
                    if begin <= float(when) < end:
                        for kind in message_types(protocol, field):
                            counts[phase, kind] = counts.get((phase, kind), 0) + 1
        rows += [{'protocol': protocol, 'failure_type': failure_type, 'run': repetition, 'phase': phase,
                  'message_type': kind, 'packets': n} for (phase, kind), n in sorted(counts.items())]
    # Reescrito a cada execução, ao contrário de append_csv: o resultado depende só dos PCAPs.
    path = ROOT / 'results/csv' / 'mensagens.csv'
    with path.open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    print(f'{len(folders)} execuções, {len(rows)} linhas em {path}')


def require(condition, message):
    if not condition: raise RuntimeError(message)


def capture_ready(path, process):
    require(process.poll() is None, 'tcpdump terminou')
    require(path.exists() and path.stat().st_size >= 24, 'aguardando cabeçalho PCAP')


def memory_bytes(value):
    match = re.fullmatch(r'([\d.]+)([A-Za-z]+)', value.strip())
    if not match: raise ValueError(value)
    units = {'B':1,'kB':1000,'MB':10**6,'GB':10**9,'KiB':1024,'MiB':1024**2,'GiB':1024**3}
    return float(match[1])*units[match[2]]


def consolidate_stats(folder, events, protocol, repetition, failure_type):
    samples = [json.loads(line) for line in (folder/'stats.jsonl').read_text().splitlines()]
    for phase in ['stable','failure']:
        begin = next(e['monotonic'] for e in events if e['name']==f'{phase}_start')
        end = next(e['monotonic'] for e in events if e['name']==f'{phase}_end')
        rows = [s for s in samples if begin <= s['monotonic'] < end]
        # Cada chamada retorna cinco linhas. Somar contêineres por amostra.
        cpus, memories = [], []
        for offset in range(0,len(rows),5):
            group = rows[offset:offset+5]
            if len(group) != 5: continue
            cpus.append(sum(float(s['stats']['CPUPerc'].rstrip('%')) for s in group))
            memories.append(sum(memory_bytes(s['stats']['MemUsage'].split('/')[0]) for s in group)/1024**2)
        if cpus:
            append_csv('resources.csv', {'protocol':protocol,'failure_type':failure_type,'run':repetition,'phase':phase,'cpu_mean_pct':statistics.mean(cpus),'cpu_peak_pct':max(cpus),'memory_mean_mib':statistics.mean(memories),'memory_peak_mib':max(memories)})


def self_check():
    assert percentile([1,2,3,4,5],.95)==4.8
    assert memory_bytes('1.5MiB')==1572864
    parsed=ping_metrics('64 bytes: icmp_seq=1 time=1.0 ms\n64 bytes: icmp_seq=2 time=3.0 ms\n3 packets transmitted, 2 received')
    assert parsed['rtt_mean_ms']==2 and round(parsed['loss_pct'])==33
    assert round(outage([(1,0),(2,.01),(5,.04),(6,.05),(6,.05)]),9)==0.02 and outage([(1,0),(2,.012),(3,.024)])==0
    assert round(outage([(1,0),(2,.012),(5,.048)]),9)==0.024
    log = ('[2026-09-26T12:00:00.100000] 7: ring51@if8: <BROADCAST,MULTICAST> mtu 1500 qdisc noqueue state DOWN group default \n'
           '    link/ether 02:42:0a:00:33:02 brd ff:ff:ff:ff:ff:ff\n'
           '[2026-09-26T12:00:00.150000] Deleted 192.168.4.0/24 nhid 20 via 10.0.51.1 dev ring51 proto ospf metric 20 \n'
           '[2026-09-26T12:00:00.350000] 192.168.4.0/24 nhid 31 via 10.0.12.2 dev ring12 proto ospf metric 20 \n')
    begin = dt.datetime(2026,9,26,12,0,0,tzinfo=dt.timezone.utc).timestamp()
    assert round(link_down(log)-begin,6)==0.1 and round(fib_change(log,'r1',begin),6)==0.35
    header = 'traceroute to 192.168.4.10 (192.168.4.10), 30 hops max, 60 byte packets\n'
    assert route_path(header+' 1  192.168.1.1  0.1 ms\n 2  10.0.51.1  30.1 ms\n 3  10.0.45.1  30.2 ms\n 4  192.168.4.10  60.3 ms\n')=='R1>R5>R4'
    assert route_path(header+' 1  192.168.1.1  0.1 ms\n 2  10.0.12.2  0.1 ms\n 3  *\n 4  10.0.34.2  0.1 ms\n 5  192.168.4.10  0.1 ms\n')=='R1>R2>R4'
    assert message_types('bgp', '2,4,4') == ['UPDATE', 'KEEPALIVE', 'KEEPALIVE'] and message_types('bgp', '') == ['TCP ACK']
    assert message_types('ospf', '4') == ['LS Update'] and message_types('rip', '2') == ['Response']
    print('PASS: parsing, percentil, unidades, outage, monitor, caminho do traceroute e tipos de mensagem')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('operation',choices=['test','collect','ready','self-check','link','selecao','mensagens'])
    parser.add_argument('protocol',nargs='?',choices=list(FILTERS),default='bgp')
    parser.add_argument('--run',type=int,default=1)
    parser.add_argument('--stable',type=int,default=60)
    parser.add_argument('--failure',type=int,default=60)
    parser.add_argument('--state',choices=['up','down'],default='up')
    parser.add_argument('--failure-type',choices=['admin','silent'],default='admin')
    args=parser.parse_args()
    os.environ['PROTOCOL']=args.protocol
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(143))
    if args.operation=='self-check': self_check()
    elif args.operation=='test': validate(args.protocol)
    elif args.operation=='ready': wait_for(lambda:ready(args.protocol))
    elif args.operation=='link': link(args.state,failure_type=args.failure_type)
    elif args.operation=='selecao': selection(args.protocol)
    elif args.operation=='mensagens': messages()
    else:
        if args.stable < 20 or args.failure < 10: parser.error('Janelas mínimas: estabilidade 20 s, falha 10 s (piloto)')
        if args.failure_type=='silent' and args.failure < 240: parser.error('Falha silenciosa exige --failure >= 240 (timeout RIP 180 s + atualização)')
        collect(args.protocol,args.run,args.stable,args.failure,args.failure_type)

if __name__=='__main__':
    main()
