import http from 'k6/http';
import { check, sleep } from 'k6';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';

const scenario = __ENV.SCENARIO || 'wave';
const workload = scenario === 'baseline' ? (__ENV.BASELINE_FOR || 'wave') : scenario;
const baseURL = (__ENV.BASE_URL || 'http://nginx/api').replace(/\/$/, '');
const postRatio = workload === 'read-heavy' ? 0.2 : 0.8;
const testid = __ENV.TESTID || `${scenario}-manual`;
if (!/^[a-zA-Z0-9_-]+$/.test(testid)) throw new Error('TESTID must be a filename-safe identifier');

const profiles = {
  smoke: [{ duration: '10s', target: 1 }],
  baseline: [{ duration: '60s', target: 50 }],
  storm: [
    { duration: '10s', target: 1000 }, { duration: '30s', target: 1000 },
    { duration: '10s', target: 50 }, { duration: '120s', target: 50 },
    { duration: '10s', target: 0 },
  ],
  wave: [
    { duration: '120s', target: 500 }, { duration: '60s', target: 500 },
    { duration: '30s', target: 50 }, { duration: '120s', target: 50 },
    { duration: '10s', target: 0 },
  ],
};
profiles['read-heavy'] = profiles.wave;
if (!profiles[scenario]) throw new Error(`Unknown SCENARIO: ${scenario}`);

export const options = {
  scenarios: { orders: {
    executor: 'ramping-vus', startVUs: scenario === 'baseline' ? 50 : scenario === 'smoke' ? 1 : 0,
    stages: profiles[scenario], gracefulRampDown: '10s', gracefulStop: '10s',
  } },
  tags: { testid, workload },
  summaryTrendStats: ['min', 'avg', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
};

const latency = {
  GET: new Trend('orders_get_success_duration', true),
  POST: new Trend('orders_post_success_duration', true),
};
const steadyLatency = {
  GET: new Trend('steady_get_success_duration', true),
  POST: new Trend('steady_post_success_duration', true),
};
const successes = {
  GET: new Counter('orders_get_succeeded'), POST: new Counter('orders_post_succeeded'),
};
const failures = new Counter('orders_failures');
const failureRate = new Rate('orders_failure_rate');
const steadyFailureRate = new Rate('steady_failure_rate');
let errorSamples = 0;

export function setup() {
  const response = http.get(`${baseURL}/users`, { tags: { name: 'setup_users' }, timeout: '10s' });
  if (response.status !== 200) throw new Error(`Cannot read seed users: HTTP ${response.status}`);
  const users = response.json();
  const ids = __ENV.USER_IDS ? __ENV.USER_IDS.split(',').map(Number) : users.map(u => u.id).slice(0, 2);
  if (ids.length !== 2 || ids.some(id => !Number.isInteger(id) || !users.some(u => u.id === id))) {
    throw new Error('Prepare two users before the run; USER_IDS must contain their actual IDs');
  }
  return { ids };
}

function phaseAt(seconds) {
  if (scenario === 'baseline') return seconds >= 30 ? 'steady' : 'warmup';
  if (scenario === 'smoke') return 'smoke';
  const storm = scenario === 'storm';
  const rampEnd = storm ? 10 : 120;
  const peakEnd = storm ? 40 : 180;
  const declineEnd = storm ? 50 : 210;
  if (seconds < rampEnd) return 'ramp';
  if (seconds < peakEnd) return 'peak';
  if (seconds < declineEnd) return 'decline';
  if (seconds < declineEnd + 60) return 'recovery_early';
  if (seconds < declineEnd + 120) return 'steady';
  return 'finish';
}

export default function(data) {
  const method = Math.random() < postRatio ? 'POST' : 'GET';
  const phase = phaseAt(exec.instance.currentTestRunDuration / 1000);
  const tags = { name: 'orders', phase, method };
  const userID = data.ids[Math.floor(Math.random() * data.ids.length)];
  const payload = JSON.stringify({ user_id: userID, amount: Math.random() * 100, description: 'hw1 load' });
  const params = { tags, timeout: '10s', headers: { 'Content-Type': 'application/json' } };
  const res = method === 'POST' ? http.post(`${baseURL}/orders`, payload, params) : http.get(`${baseURL}/orders`, params);
  let valid = false;
  try {
    const body = res.json();
    valid = method === 'POST'
      ? (res.status === 200 || res.status === 201) && Number.isInteger(body.id) && body.user_id === userID
      : res.status === 200 && Array.isArray(body);
  } catch (_) { /* Non-JSON error responses are failed checks. */ }
  check(res, { 'order response valid': () => valid }, tags);
  failureRate.add(!valid, tags);
  if (phase === 'steady') steadyFailureRate.add(!valid, tags);
  if (valid) {
    latency[method].add(res.timings.duration, tags);
    successes[method].add(1, tags);
    if (phase === 'steady') steadyLatency[method].add(res.timings.duration, tags);
  } else {
    failures.add(1, { ...tags, status: String(res.status) });
    if (__VU <= 3 && errorSamples++ < 3) console.warn(`HTTP ${res.status} ${method}: ${String(res.body).slice(0, 300)}`);
  }
  sleep(0.05);
}

export function handleSummary(data) {
  data.hw1 = { scenario, workload, testid, postRatio, baseURL, stages: profiles[scenario] };
  return {
    [`/results/${testid}.json`]: JSON.stringify(data, null, 2),
    stdout: JSON.stringify({ testid, metrics: data.metrics }, null, 2) + '\n',
  };
}
