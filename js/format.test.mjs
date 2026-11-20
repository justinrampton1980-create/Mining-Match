// Date maths used by the web app to suggest licence expiry dates.
// Run: node --test   (from the js/ folder, Node 18+)
import test from 'node:test';
import assert from 'node:assert/strict';
import { addYears, parseDate, isoDate } from '../web/js/format.js';

test('adds whole years to an issue date', () => {
  assert.equal(addYears('2024-03-10', 5), '2029-03-10');
  assert.equal(addYears('2026-12-31', 3), '2029-12-31');
  assert.equal(addYears('2025-01-01', 1), '2026-01-01');
});

test('29 Feb becomes 28 Feb in a year without one', () => {
  assert.equal(addYears('2024-02-29', 5), '2029-02-28');
  assert.equal(addYears('2024-02-29', 3), '2027-02-28');
  assert.equal(addYears('2024-02-29', 4), '2028-02-29'); // leap year: stays on the 29th
});

test('no date gives no suggestion', () => {
  assert.equal(addYears('', 5), '');
  assert.equal(addYears(null, 5), '');
  assert.equal(addYears('not a date', 5), '');
});

test('dates round-trip without time-zone shifts', () => {
  assert.equal(isoDate(parseDate('2026-11-20')), '2026-11-20');
  assert.equal(isoDate(parseDate('2026-11-20 09:00:00')), '2026-11-20');
});
