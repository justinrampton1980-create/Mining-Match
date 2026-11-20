'use strict';
// Run: node --test   (from the js/ folder, Node 18+)
const test = require('node:test');
const assert = require('node:assert/strict');
const me = require('./matchingEngine');
const FIXTURES = require('../fixtures/match_cases.json');

const JOB = FIXTURES.job;
const JOB2 = FIXTURES.job2;
const AS_OF = FIXTURES.as_of;
const CANDS = Object.fromEntries(FIXTURES.candidates.map((c) => [c.id, c]));

const score = (cid, job = JOB, opts = {}) =>
  me.scoreCandidate(CANDS[cid], job, { asOf: AS_OF, ...opts });
const ids = (results, key = 'candidate_id') => results.map((r) => r[key]);

// ---- shared fixture parity (same expectations as the Python suite) ----
test('each candidate matches the shared fixture expectations', () => {
  for (const [cid, exp] of Object.entries(FIXTURES.expected)) {
    const r = score(cid);
    assert.equal(r.score, exp.score, `${cid} score`);
    assert.equal(r.eligible, exp.eligible, `${cid} eligible`);
    assert.deepEqual(r.breakdown, exp.breakdown, `${cid} breakdown`);
    assert.deepEqual(r.blockers, exp.blockers, `${cid} blockers`);
    assert.deepEqual(r.gaps, exp.gaps, `${cid} gaps`);
    assert.deepEqual(r.warnings, exp.warnings, `${cid} warnings`);
  }
});

test('ranking order matches the shared fixture', () => {
  const ranked = me.rankCandidates(FIXTURES.candidates, JOB, { asOf: AS_OF });
  assert.deepEqual(ids(ranked), FIXTURES.expected_order);
});

// ---- licences ----
test('aliases and spelling are normalised', () => {
  assert.equal(me.normalize('SS11'), 'standard 11');
  assert.equal(me.normalize('  Heavy   Rigid License '), 'heavy rigid licence');
});

test('licence expiring on the start date is still valid (with a warning)', () => {
  const cand = { licences: [{ type: 'Standard 11', expiry: '2026-12-01' }] };
  const job = { start_date: '2026-12-01', required_licences: ['Standard 11'] };
  const r = me.scoreCandidate(cand, job);
  assert.equal(r.eligible, true);
  assert.deepEqual(r.warnings, ['Standard 11 expires 2026-12-01']);
});

test('licence expired the day before start is a blocker', () => {
  const cand = { licences: [{ type: 'Standard 11', expiry: '2026-11-30' }] };
  const job = { start_date: '2026-12-01', required_licences: ['Standard 11'] };
  const r = me.scoreCandidate(cand, job);
  assert.equal(r.eligible, false);
  assert.deepEqual(r.blockers, ['Standard 11: expired 2026-11-30']);
});

test('latest expiry wins when a licence is held twice', () => {
  const cand = {
    licences: [
      { type: 'Standard 11', expiry: '2020-01-01' },
      { type: 'Standard 11', expiry: '2030-01-01' },
    ],
  };
  const job = { start_date: '2026-12-01', required_licences: ['Standard 11'] };
  assert.equal(me.scoreCandidate(cand, job).eligible, true);
});

test('custom alias registration', () => {
  me.registerAliases({ 'cert iii se': 'cert iii surface extraction' });
  const cand = { qualifications: ['Cert III SE'] };
  const job = { required_qualifications: ['Cert III Surface Extraction'] };
  assert.equal(me.scoreCandidate(cand, job, { asOf: AS_OF }).eligible, true);
});

// ---- weights ----
test('custom weights: licences only', () => {
  const r = score('cand-b', JOB, {
    weights: { qualifications: 0, skills: 0, experience: 0, location: 0 },
  });
  assert.equal(r.score, 40.0);
});

test('component is skipped when the job has no requirement for it', () => {
  const job = { ...JOB };
  delete job.location;
  const r = score('cand-d', job);
  assert.equal(r.breakdown.location, null);
  assert.equal(r.score, 78.9); // 0.75 / 0.95 renormalised
});

test('invalid weights are rejected', () => {
  assert.throws(() => score('cand-a', JOB, { weights: { licences: -1 } }));
  assert.throws(() => score('cand-a', JOB, { weights: { salary: 1 } }));
});

test('job with no requirements scores 0 with a warning', () => {
  const r = me.scoreCandidate(CANDS['cand-a'], { id: 'empty' }, { asOf: AS_OF });
  assert.equal(r.score, 0.0);
  assert.equal(r.eligible, true);
  assert.deepEqual(r.warnings, ['job has no scorable requirements']);
});

// ---- ranking ----
test('eligibleOnly filter', () => {
  const ranked = me.rankCandidates(FIXTURES.candidates, JOB, { asOf: AS_OF, eligibleOnly: true });
  assert.deepEqual(ids(ranked), ['cand-a', 'cand-c']);
});

test('minScore and limit', () => {
  let ranked = me.rankCandidates(FIXTURES.candidates, JOB, { asOf: AS_OF, minScore: 80 });
  assert.deepEqual(ids(ranked), ['cand-a', 'cand-c']);
  ranked = me.rankCandidates(FIXTURES.candidates, JOB, { asOf: AS_OF, limit: 1 });
  assert.deepEqual(ids(ranked), ['cand-a']);
});

test('ineligible ranks below eligible even with a higher score', () => {
  // Skills-only weighting: d scores 100 (but lacks a required cert), c scores 66.7.
  const onlySkills = { licences: 0, qualifications: 0, experience: 0, location: 0 };
  const ranked = me.rankCandidates([CANDS['cand-d'], CANDS['cand-c']], JOB, {
    weights: onlySkills,
    asOf: AS_OF,
  });
  assert.deepEqual(ranked.map((r) => r.score), [66.7, 100.0]);
  assert.deepEqual(ids(ranked), ['cand-c', 'cand-d']);
});

test('jobs for a candidate', () => {
  const ranked = me.matchJobsForCandidate(CANDS['cand-a'], [JOB2, JOB], { asOf: AS_OF });
  assert.deepEqual(ids(ranked, 'job_id'), ['job-1', 'job-2']);
  assert.equal(ranked[1].score, 40.0);
  assert.equal(ranked[1].eligible, false);
});

test('results are JSON-serialisable', () => {
  JSON.stringify(score('cand-b'));
});
