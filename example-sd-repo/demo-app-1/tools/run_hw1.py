#!/usr/bin/env python3
"""Run HW1 against the isolated 'hw1' Compose project; save evidence, not conclusions."""
import argparse
import datetime as dt
import json
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = APP.parents[1] / 'hws' / 'hw1-results'
COMPOSE = ['docker', 'compose', '-p', 'hw1', '-f', str(APP / 'docker-compose.yaml')]
PROMETHEUS = 'http://localhost:9090'


def command(args, **kwargs):
    return subprocess.run(args, cwd=APP, check=True, text=True, capture_output=True, **kwargs).stdout.strip()


def get_json(url):
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.load(response)


def seed():
    # Fixed project and database: never reset a user's existing Compose deployment.
    sql = '''BEGIN;
TRUNCATE orders, users RESTART IDENTITY;
INSERT INTO users (name, email) VALUES ('HW1 Alice', 'hw1-alice@example.com'), ('HW1 Bob', 'hw1-bob@example.com');
INSERT INTO orders (user_id, amount, description)
SELECT (SELECT id FROM users ORDER BY id LIMIT 1 OFFSET (n % 2)), 10.00, 'hw1 seed'
FROM generate_series(1, 1000) AS n;
COMMIT;
SELECT string_agg(id::text, ',' ORDER BY id) FROM users;'''
    result = command(COMPOSE + ['exec', '-T', 'db', 'psql', '-U', 'demo', '-d', 'demo', '-v', 'ON_ERROR_STOP=1', '-At'], input=sql)
    ids = result.splitlines()[-1]
    if not re.fullmatch(r'\d+,\d+', ids):
        raise RuntimeError(f'Invalid seed IDs: {ids}')
    return ids


def resource_samples(path, stop):
    with path.open('w') as output:
        while not stop.is_set():
            started = time.time()
            try:
                ids = command(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=hw1']).splitlines()
                if ids:
                    lines = command(['docker', 'stats', '--no-stream', '--format', '{{json .}}'] + ids, timeout=15)
                    for line in lines.splitlines():
                        output.write(json.dumps({'timestamp': time.time(), **json.loads(line)}) + '\n')
                    output.flush()
            except Exception as error:
                output.write(json.dumps({'timestamp': time.time(), 'sampling_error': str(error)}) + '\n')
            stop.wait(max(0, 5 - (time.time() - started)))


QUERIES = {
    'pool_open': 'db_pool_open_connections',
    'pool_in_use': 'db_pool_in_use_connections',
    'pool_idle': 'db_pool_idle_connections',
    'pool_wait_count': 'db_pool_wait_count_total',
    'pool_wait_seconds': 'db_pool_wait_duration_seconds_total',
    'pg_connections': 'pg_stat_database_numbackends{datname="demo"}',
    'pg_up': 'pg_up',
    'pg_scrape_error': 'pg_exporter_last_scrape_error',
    'pg_deadlocks': 'pg_stat_database_deadlocks{datname="demo"}',
    'pg_transactions_rps': 'rate(pg_stat_database_xact_commit{datname="demo"}[30s])',
    'pg_cache_hit_ratio': 'pg_stat_database_blks_hit{datname="demo"} / (pg_stat_database_blks_hit{datname="demo"} + pg_stat_database_blks_read{datname="demo"})',
    'sql_p95': 'histogram_quantile(0.95, sum(rate(db_query_duration_seconds_bucket[30s])) by (le))',
    'backend_rps': 'sum(rate(http_requests_total{path="/api/orders"}[30s])) by (method)',
    'backend_success_rps': 'sum(rate(http_requests_total{path="/api/orders",status=~"2.."}[30s])) by (method)',
    'backend_errors_rps': 'sum(rate(http_requests_total{path="/api/orders",status!~"2.."}[30s])) by (method,status)',
    'backend_success_p95': 'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{path="/api/orders",status=~"2.."}[30s])) by (method,le))',
    'backend_success_p99': 'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{path="/api/orders",status=~"2.."}[30s])) by (method,le))',
    'backend_memory': 'process_resident_memory_bytes{job="backend"}',
    'backend_cpu_cores': 'rate(process_cpu_seconds_total{job="backend"}[30s])',
    'k6_vus': 'k6_vus{testid="TESTID"}',
    'k6_rps': 'sum(rate(k6_http_reqs_total{testid="TESTID",name="orders"}[30s])) by (method)',
    'k6_errors': 'k6_orders_failures_total{testid="TESTID"}',
    'k6_success_p95': '{__name__=~"k6_orders_(get|post)_success_duration_p95",testid="TESTID"}',
    'targets': 'up',
}


def save_prometheus(path, start, end, testid):
    evidence = {}
    for name, expression in QUERIES.items():
        params = urllib.parse.urlencode({'query': expression.replace('TESTID', testid), 'start': start, 'end': end, 'step': 5})
        evidence[name] = get_json(f'{PROMETHEUS}/api/v1/query_range?{params}')
    path.write_text(json.dumps(evidence, indent=2) + '\n')


def run_test(output, scenario, ids, baseline_for=None):
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    testid = f'{scenario}-{baseline_for or scenario}-{stamp}'
    directory = output / testid
    directory.mkdir(parents=True)
    stop = threading.Event()
    sampler = threading.Thread(target=resource_samples, args=(directory / 'resources.jsonl', stop), daemon=True)
    sampler.start()
    start = time.time()
    args = COMPOSE + ['run', '--rm', '--no-deps', '-e', f'SCENARIO={scenario}', '-e', f'TESTID={testid}', '-e', f'USER_IDS={ids}']
    if baseline_for:
        args += ['-e', f'BASELINE_FOR={baseline_for}']
    args += ['k6', 'run', '--out', 'experimental-prometheus-rw', '/scripts/load-script.js']
    print(f'START {testid}', flush=True)
    try:
        with (directory / 'k6.log').open('w') as log:
            result = subprocess.run(args, cwd=APP, stdout=log, stderr=subprocess.STDOUT)
    finally:
        end = time.time()
        stop.set()
        sampler.join(timeout=20)
    for service in ('backend', 'db', 'nginx', 'postgres_exporter'):
        log = command(COMPOSE + ['logs', '--no-color', '--tail', '10000', '--since', dt.datetime.fromtimestamp(start, dt.timezone.utc).isoformat(), service])
        (directory / f'{service}.log').write_text(log + '\n')
    summary = APP / 'k6' / 'results' / f'{testid}.json'
    if summary.exists():
        (directory / 'summary.json').write_text(summary.read_text())
    metadata = {'testid': testid, 'scenario': scenario, 'baseline_for': baseline_for, 'start': start, 'end': end,
                'exit_code': result.returncode, 'user_ids': ids, 'command': args}
    (directory / 'run.json').write_text(json.dumps(metadata, indent=2) + '\n')
    save_prometheus(directory / 'prometheus.json', start - 10, end + 5, testid)
    print(f'FINISH {testid}: exit={result.returncode}, elapsed={end-start:.1f}s', flush=True)
    if result.returncode != 0 or not summary.exists():
        raise RuntimeError(f'Run failed; see {directory / "k6.log"}')
    return testid


def environment(output):
    info = json.loads(command(['docker', 'info', '--format', '{{json .}}']))
    selected = {key: info.get(key) for key in ('ServerVersion', 'Architecture', 'NCPU', 'MemTotal', 'KernelVersion', 'OperatingSystem')}
    selected['compose_version'] = command(['docker', 'compose', 'version'])
    selected['host_platform'] = platform.platform()
    if platform.system() == 'Darwin':
        selected['host_cpu_count'] = int(command(['sysctl', '-n', 'hw.ncpu']))
        selected['host_memory_bytes'] = int(command(['sysctl', '-n', 'hw.memsize']))
    selected['colima'] = command(['colima', 'list']) if shutil.which('colima') else None
    selected['containers'] = command(COMPOSE + ['ps', '--format', 'json'])
    selected['image_digests'] = []
    images = command(COMPOSE + ['images', '-q']).splitlines()
    if images:
        selected['image_digests'] = command(['docker', 'image', 'inspect', *images, '--format', '{{.RepoTags}} {{.RepoDigests}}'])
    try:
        generator_digest = command(['docker', 'image', 'inspect', 'grafana/k6:0.57.0', '--format', '{{.RepoDigests}}'])
    except subprocess.CalledProcessError:
        command(COMPOSE + ['pull', 'k6'])
        generator_digest = command(['docker', 'image', 'inspect', 'grafana/k6:0.57.0', '--format', '{{.RepoDigests}}'])
    selected['generator_image_digest'] = generator_digest
    selected['postgres'] = command(COMPOSE + ['exec', '-T', 'db', 'psql', '-U', 'demo', '-d', 'demo', '-Atc', 'SELECT version(); SHOW max_connections;'])
    selected['backend_pool'] = 'default database/sql: unlimited max open connections; max idle connections=2'
    (output / 'environment.json').write_text(json.dumps(selected, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--scenario', choices=['all', 'smoke', 'storm', 'wave', 'read-heavy'], default='all')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    environment(output)
    if args.scenario == 'smoke':
        run_test(output, 'smoke', seed())
        return
    for scenario in (['storm', 'wave', 'read-heavy'] if args.scenario == 'all' else [args.scenario]):
        ids = seed()
        baseline = run_test(output, 'baseline', ids, scenario)
        experiment = run_test(output, scenario, ids)
        with (output / 'pairs.jsonl').open('a') as pairs:
            pairs.write(json.dumps({'scenario': scenario, 'baseline': baseline, 'experiment': experiment}) + '\n')


if __name__ == '__main__':
    main()
