// Самопроверка сглаживания Motion: node services/dashboard/tools/motion_check.mjs
// ТС едет 500 м за 20 с потока и встаёт: маркер должен доехать без перелёта и без скачков скорости.
import assert from 'node:assert/strict';
import { RouteLine, Motion } from '../js/motion.js';

const line = new RouteLine([[37.6, 55.7], [37.7, 55.7]]);
const M = new Motion();
M.clock(1000, 0);
for (const [m, ts] of [[0, 1000], [500, 1020], [500, 1040]]) M.push('x', { line, m, lat: 55.7, lon: 37.6, ts });
Object.assign(M, { lagTarget: 0, lagCur: 0, lagWall: 0, rate: 1 });

let prev = null;
let prevV = 0;
let maxM = 0;
let maxDv = 0;
for (let i = 0; i < 60 * 30; i++) {
  M.anchor = { c0: 1000, w0: 0, corr: 0, lastStream: 1000, lastWall: 0 };
  const { m } = M.sample('x', (i * 1000) / 60);
  if (prev != null) {
    const v = m - prev;
    if (i > 1) maxDv = Math.max(maxDv, Math.abs(v - prevV));
    prevV = v;
  }
  prev = m;
  maxM = Math.max(maxM, m);
}
assert.ok(maxM <= 500.01, `перелёт: ${maxM}`);
assert.ok(Math.abs(prev - 500) < 0.05, `не доехал: ${prev}`);
// линейная интерполяция меняет скорость скачком на 0.417 м/кадр
assert.ok(maxDv < 0.05, `рывок скорости: ${maxDv}`);
console.log('motion ok');
