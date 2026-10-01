#!/usr/bin/env python3
"""Summarize completed HW1 pairs and plot their saved Prometheus/resource samples."""
import argparse
import json
import math
from pathlib import Path

DEFAULT_RESULTS = Path(__file__).resolve().parents[3] / 'hws' / 'hw1-results'


def values(summary, name):
    return summary.get('metrics', {}).get(name, {}).get('values', {})


def series(prometheus, name):
    return prometheus.get(name, {}).get('data', {}).get('result', [])


def samples(prometheus, name):
    return [float(v) for s in series(prometheus, name) for _, v in s.get('values', []) if math.isfinite(float(v))]


def maximum(prometheus, name):
    points = samples(prometheus, name)
    return max(points) if points else None


def cpu_samples(directory):
    result = {}
    for line in (directory / 'resources.jsonl').read_text().splitlines():
        sample = json.loads(line)
        if 'sampling_error' in sample:
            continue
        name = sample['Name']
        service = 'k6' if name.startswith('hw1-k6-run-') else name.removeprefix('hw1-').removesuffix('-1')
        result.setdefault(service, []).append(sample)
    return result


def summarize(directory):
    summary = json.loads((directory / 'summary.json').read_text())
    prom = json.loads((directory / 'prometheus.json').read_text())
    run = json.loads((directory / 'run.json').read_text())
    for query in prom.values():
        for item in query.get('data', {}).get('result', []):
            item['values'] = [(t, v) for t, v in item['values'] if run['start'] <= t <= run['end']]
    requests = values(summary, 'orders_failure_rate')
    successes = {method: values(summary, f'orders_{method.lower()}_succeeded').get('count', 0) for method in ('GET', 'POST')}
    resources = cpu_samples(directory)
    total = requests.get('passes', 0) + requests.get('fails', 0)
    # Rates from different methods are summed at matching timestamps, not by summing their maxima.
    by_time = {}
    for s in series(prom, 'backend_success_rps'):
        for t, v in s['values']:
            if run['start'] <= t <= run['end'] and math.isfinite(float(v)):
                by_time[t] = by_time.get(t, 0) + float(v)
    return {
        'testid': run['testid'], 'start': run['start'], 'end': run['end'], 'exit_code': run['exit_code'],
        'requests': total, 'successful_requests': sum(successes.values()), 'successes_by_method': successes,
        'success_rps': sum(values(summary, f'orders_{m.lower()}_succeeded').get('rate', 0) for m in ('GET', 'POST')),
        'error_rate': requests.get('rate'), 'steady_error_rate': values(summary, 'steady_failure_rate').get('rate'),
        'peak_success_rps_30s': max(by_time.values()) if by_time else None,
        'max_vus': maximum(prom, 'k6_vus'), 'max_pool_open': maximum(prom, 'pool_open'),
        'max_pool_in_use': maximum(prom, 'pool_in_use'), 'max_pg_connections': maximum(prom, 'pg_connections'),
        'max_pool_wait_count': maximum(prom, 'pool_wait_count'), 'max_pool_wait_seconds': maximum(prom, 'pool_wait_seconds'),
        'max_sql_p95_seconds': maximum(prom, 'sql_p95'),
        'pg_connection_metric_missing_samples': max(0, len(samples(prom, 'pg_up')) - len(samples(prom, 'pg_connections'))),
        'latency_ms': {m: values(summary, f'orders_{m.lower()}_success_duration') for m in ('GET', 'POST')},
        'steady_latency_ms': {m: values(summary, f'steady_{m.lower()}_success_duration') for m in ('GET', 'POST')},
        'max_cpu_percent': {s: max(float(p['CPUPerc'].rstrip('%')) for p in points) for s, points in resources.items()},
        'max_memory_usage': {s: max(points, key=lambda p: memory_bytes(p['MemUsage'].split('/')[0].strip()))['MemUsage'].split('/')[0].strip() for s, points in resources.items()},
        'pg_deadlocks_delta': (max(samples(prom, 'pg_deadlocks')) - min(samples(prom, 'pg_deadlocks'))) if samples(prom, 'pg_deadlocks') else None,
        'negative_timing_minima_ms': {name: metric['values']['min'] for name, metric in summary['metrics'].items()
                                     if metric.get('type') == 'trend' and metric['values'].get('min', 0) < 0},
    }


def memory_bytes(text):
    units = {'B':1, 'kB':1000, 'MB':1e6, 'GB':1e9, 'KiB':1024, 'MiB':1024**2, 'GiB':1024**3}
    for unit in sorted(units, key=len, reverse=True):
        if text.endswith(unit):
            return float(text[:-len(unit)]) * units[unit]
    raise ValueError(text)


def plot(directory, destination, title):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    run = json.loads((directory / 'run.json').read_text())
    prom = json.loads((directory / 'prometheus.json').read_text())
    start = run['start']
    fig, axes = plt.subplots(3, 2, figsize=(13, 10), sharex=True, constrained_layout=True)
    panels = [
        ('k6_vus', 'Virtual users', 'VU'),
        ('backend_success_rps', 'Successful throughput (30 s rate)', 'requests/s'),
        ('backend_success_p95', 'Successful backend latency p95 (30 s histogram)', 'seconds'),
        ('backend_errors_rps', 'Backend errors (30 s rate)', 'errors/s'),
        ('pool_open', 'Application pool (includes pending opens) / PostgreSQL', 'connections'),
    ]
    for ax, (name, label, unit) in zip(axes.flat, panels):
        for item in series(prom, name):
            labels = item['metric']
            legend = labels.get('method', labels.get('status', name))
            if name == 'backend_errors_rps':
                legend = f"{labels.get('method','')} {labels.get('status','')}"
            xs, ys = [], []
            for t,v in item['values']:
                if math.isfinite(float(v)):
                    xs.append(t-start); ys.append(float(v))
            ax.plot(xs, ys, label=legend)
        if name == 'pool_open':
            for other,label2 in [('pool_in_use','pool non-idle'),('pg_connections','PostgreSQL (gaps = missing data)')]:
                for item in series(prom,other):
                    xs, ys = [], []
                    previous = None
                    for t,v in item['values']:
                        if previous is not None and t-previous > 5.1:
                            xs.append(t-start); ys.append(float('nan'))
                        xs.append(t-start); ys.append(float(v)); previous=t
                    ax.plot(xs,ys,label=label2)
            ax.axhline(200, color='tab:red', linestyle=':', label='PG max_connections=200')
        ax.set_title(label); ax.set_ylabel(unit); ax.grid(alpha=.2)
        if ax.lines: ax.legend(fontsize=8)
        else: ax.text(.5,.5,'No error series observed',transform=ax.transAxes,ha='center')
    cpu_ax = axes.flat[5]
    for service, points in cpu_samples(directory).items():
        if service in ('backend','db','k6','nginx','prometheus'):
            cpu_ax.plot([p['timestamp']-start for p in points], [float(p['CPUPerc'].rstrip('%')) for p in points],label=service)
    cpu_ax.set_title('Container CPU (100% = one core)'); cpu_ax.set_ylabel('%'); cpu_ax.grid(alpha=.2); cpu_ax.legend(fontsize=8)
    for ax in axes.flat:
        ax.set_xlim(0, run['end']-start)
        if run['scenario'] in ('storm','wave','read-heavy'):
            boundaries = [10,40,50,110,170] if run['scenario']=='storm' else [120,180,210,270,330]
            for boundary in boundaries: ax.axvline(boundary,color='gray',alpha=.25,linewidth=.8)
    axes[2,0].set_xlabel('Seconds from Compose run invocation'); axes[2,1].set_xlabel('Seconds from Compose run invocation')
    fig.suptitle(title,fontsize=16)
    fig.savefig(destination,dpi=150)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results',type=Path,nargs='?',default=DEFAULT_RESULTS)
    parser.add_argument('--plots',action='store_true',help='Requires matplotlib')
    args = parser.parse_args()
    pairs = [json.loads(line) for line in (args.results / 'pairs.jsonl').read_text().splitlines()]
    analysis = []
    for pair in pairs:
        baseline = summarize(args.results / pair['baseline'])
        experiment = summarize(args.results / pair['experiment'])
        ratios = {}
        for method in ('GET','POST'):
            before = baseline['steady_latency_ms'][method].get('p(95)')
            after = experiment['steady_latency_ms'][method].get('p(95)')
            ratios[method] = after/before if before and after is not None else None
        recovered = (experiment['steady_error_rate'] is not None and experiment['steady_error_rate'] <= .01
                     and all(r is not None and r <= 1.2 for r in ratios.values()))
        item = {'scenario': pair['scenario'], 'baseline': baseline, 'experiment': experiment,
                'recovery_p95_ratio': ratios, 'recovered_by_criterion': recovered}
        analysis.append(item)
        if args.plots:
            plot(args.results / pair['experiment'], args.results / f"{pair['scenario']}.png", pair['scenario'])
    (args.results / 'analysis.json').write_text(json.dumps(analysis,indent=2)+'\n')
    print(json.dumps(analysis,indent=2))


if __name__ == '__main__':
    main()
