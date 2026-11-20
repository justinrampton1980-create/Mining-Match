'use strict';
/**
 * Mining Match - candidate/job matching engine (JavaScript, Node 18+).
 *
 * Mirrors python/matching_engine.py. Inputs and result objects use the same
 * snake_case shapes as the Python version (so JSON from the data layer works
 * in either language); function names are camelCase. Both are tested against
 * fixtures/match_cases.json so scores stay identical.
 *
 * Five components (licences, qualifications, skills, experience, location),
 * each 0..1, skipped when the job has no requirement for it (weights are
 * renormalised). Required items count 1.0, preferred 0.5. Overall 0..100.
 * Eligibility is separate: any missing/expired required licence (judged on the
 * job start date) or missing required qualification => not eligible.
 */

const DEFAULT_WEIGHTS = {
  licences: 0.3,
  qualifications: 0.2,
  skills: 0.25,
  experience: 0.2,
  location: 0.05,
};
const EXPIRY_WARNING_DAYS = 90;
const PREFERRED_FACTOR = 0.5;
const RELOCATION_FACTOR = 0.7;
const DAY_MS = 86400000;
const NO_EXPIRY = Infinity;

const ALIASES = {
  ss11: 'standard 11',
  s11: 'standard 11',
  hr: 'heavy rigid licence',
  'hr licence': 'heavy rigid licence',
  'heavy rigid': 'heavy rigid licence',
  cbm: 'coal board medical',
  wah: 'working at heights',
};

// ---------------------------------------------------------------- helpers
function clean(text) {
  return String(text == null ? '' : text)
    .trim()
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .replace(/license/g, 'licence');
}

function normalize(text) {
  const t = clean(text);
  return Object.prototype.hasOwnProperty.call(ALIASES, t) ? ALIASES[t] : t;
}

/** Add aliases, e.g. { 'cert iii': 'certificate iii' }. */
function registerAliases(mapping) {
  for (const [alias, canonical] of Object.entries(mapping)) {
    ALIASES[clean(alias)] = clean(canonical);
  }
}

/** Date-ish value -> UTC midnight in ms (or null). */
function toDay(value) {
  if (value == null || value === '') return null;
  if (value instanceof Date) {
    return Date.UTC(value.getUTCFullYear(), value.getUTCMonth(), value.getUTCDate());
  }
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value));
  if (!m) throw new Error(`Invalid date: ${value}`);
  return Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

function todayUtc() {
  return toDay(new Date());
}

function isoDay(ms) {
  return new Date(ms).toISOString().slice(0, 10);
}

function round1(x) {
  return Math.floor(x * 10 + 0.5) / 10;
}

function weighted(parts) {
  const total = parts.reduce((s, [w]) => s + w, 0);
  return total === 0 ? null : parts.reduce((s, [w, v]) => s + w * v, 0) / total;
}

function tiers(job, requiredKey, preferredKey) {
  return [
    ...(job[requiredKey] || []).map((item) => [item, true]),
    ...(job[preferredKey] || []).map((item) => [item, false]),
  ];
}

const tierWeight = (required) => (required ? 1.0 : PREFERRED_FACTOR);

// Each component returns { score, blockers, gaps, warnings }
const component = (score, blockers = [], gaps = [], warnings = []) => ({
  score,
  blockers,
  gaps,
  warnings,
});

// ------------------------------------------------------------- components
function heldLicences(candidate) {
  const held = new Map();
  for (const item of candidate.licences || []) {
    let name;
    let expiry = NO_EXPIRY;
    if (item && typeof item === 'object') {
      name = item.type || item.name;
      const d = toDay(item.expiry);
      if (d != null) expiry = d;
    } else {
      name = item;
    }
    const key = normalize(name);
    held.set(key, Math.max(held.has(key) ? held.get(key) : -Infinity, expiry));
  }
  return held;
}

function licenceStatus(held, key, ref) {
  if (!held.has(key)) return ['missing', null];
  const expiry = held.get(key);
  if (expiry < ref) return ['expired', expiry];
  if (expiry !== NO_EXPIRY && (expiry - ref) / DAY_MS <= EXPIRY_WARNING_DAYS) {
    return ['expiring', expiry];
  }
  return ['valid', expiry];
}

function scoreLicences(candidate, job, ref) {
  const held = heldLicences(candidate);
  const parts = [];
  const blockers = [];
  const gaps = [];
  const warnings = [];
  for (const [item, required] of tiers(job, 'required_licences', 'preferred_licences')) {
    const [status, expiry] = licenceStatus(held, normalize(item), ref);
    const ok = status === 'valid' || status === 'expiring';
    parts.push([tierWeight(required), ok ? 1.0 : 0.0]);
    if (status === 'expiring') warnings.push(`${item} expires ${isoDay(expiry)}`);
    if (!ok) {
      const reason = status === 'missing' ? 'missing' : `expired ${isoDay(expiry)}`;
      if (required) blockers.push(`${item}: ${reason}`);
      else gaps.push(`${item} (preferred): ${reason}`);
    }
  }
  return component(weighted(parts), blockers, gaps, warnings);
}

function scoreQualifications(candidate, job) {
  const held = new Set(
    (candidate.qualifications || []).map((q) =>
      normalize(q && typeof q === 'object' ? q.name : q)
    )
  );
  const parts = [];
  const blockers = [];
  const gaps = [];
  for (const [item, required] of tiers(job, 'required_qualifications', 'preferred_qualifications')) {
    const ok = held.has(normalize(item));
    parts.push([tierWeight(required), ok ? 1.0 : 0.0]);
    if (!ok) {
      if (required) blockers.push(`${item}: missing`);
      else gaps.push(`${item} (preferred): missing`);
    }
  }
  return component(weighted(parts), blockers, gaps);
}

function heldSkills(candidate) {
  const skills = new Map();
  for (const item of candidate.skills || []) {
    let name;
    let years = null;
    if (item && typeof item === 'object') {
      name = item.name;
      years = item.years == null ? null : item.years;
    } else {
      name = item;
    }
    const key = normalize(name);
    if (
      !skills.has(key) ||
      (years !== null && (skills.get(key) === null || years > skills.get(key)))
    ) {
      skills.set(key, years);
    }
  }
  return skills;
}

function scoreSkills(candidate, job) {
  const held = heldSkills(candidate);
  const parts = [];
  const gaps = [];
  for (const [item, required] of tiers(job, 'required_skills', 'preferred_skills')) {
    let name;
    let minYears = null;
    if (item && typeof item === 'object') {
      name = item.name;
      minYears = item.min_years == null ? null : item.min_years;
    } else {
      name = item;
    }
    const key = normalize(name);
    const label = required ? name : `${name} (preferred)`;
    let value;
    if (!held.has(key)) {
      value = 0.0;
      gaps.push(`${label}: missing`);
    } else {
      const years = held.get(key);
      // Unknown years (plain skill name) gets full credit for presence.
      value = years === null || !minYears ? 1.0 : Math.min(1.0, years / minYears);
      if (value < 1.0) gaps.push(`${label}: ${years}/${minYears} yrs`);
    }
    parts.push([tierWeight(required), value]);
  }
  return component(weighted(parts), [], gaps);
}

function scoreExperience(candidate, job) {
  const minYears = job.min_experience_years;
  if (!minYears) return component(null);
  const roles = new Set((job.relevant_roles || []).map(normalize));
  let total = 0;
  for (const entry of candidate.experience || []) {
    if (roles.size === 0 || roles.has(normalize(entry.role))) {
      total += Number(entry.years) || 0;
    }
  }
  const value = Math.min(1.0, total / minYears);
  const gaps = value < 1.0 ? [`experience: ${total}/${minYears} yrs relevant`] : [];
  return component(value, [], gaps);
}

function scoreLocation(candidate, job) {
  const want = normalize(job.location);
  if (!want) return component(null);
  const have = normalize(candidate.location);
  if (have === want) return component(1.0);
  if (candidate.willing_to_relocate || candidate.willing_fifo) {
    return component(RELOCATION_FACTOR, [], [], ['relocation or FIFO required']);
  }
  const shown = candidate.location || 'unknown';
  return component(0.0, [], [`location: ${shown} (job in ${job.location})`]);
}

// -------------------------------------------------------------- public API
/**
 * Score one candidate against one job.
 * Licence validity is judged on the job's start_date (falling back to asOf,
 * then today) so a licence that lapses before the start doesn't count.
 *
 * @returns {{candidate_id, job_id, score, eligible, breakdown, blockers, gaps, warnings}}
 */
function scoreCandidate(candidate, job, { weights, asOf } = {}) {
  const w = { ...DEFAULT_WEIGHTS, ...(weights || {}) };
  for (const [key, value] of Object.entries(w)) {
    if (!(key in DEFAULT_WEIGHTS)) throw new Error(`Unknown weight: ${key}`);
    if (!(value >= 0)) throw new Error('Weights must be >= 0');
  }

  const ref = toDay(job.start_date) ?? toDay(asOf) ?? todayUtc();
  const results = {
    licences: scoreLicences(candidate, job, ref),
    qualifications: scoreQualifications(candidate, job),
    skills: scoreSkills(candidate, job),
    experience: scoreExperience(candidate, job),
    location: scoreLocation(candidate, job),
  };

  const active = Object.entries(results).filter(([k, r]) => r.score !== null && w[k] > 0);
  const totalWeight = active.reduce((s, [k]) => s + w[k], 0);
  const overall = totalWeight
    ? (100 * active.reduce((s, [k, r]) => s + w[k] * r.score, 0)) / totalWeight
    : 0.0;

  const all = Object.values(results);
  const blockers = all.flatMap((r) => r.blockers);
  const gaps = all.flatMap((r) => r.gaps);
  const warnings = all.flatMap((r) => r.warnings);
  if (active.length === 0) warnings.push('job has no scorable requirements');

  const breakdown = {};
  for (const [k, r] of Object.entries(results)) {
    breakdown[k] = r.score === null ? null : round1(r.score * 100);
  }

  return {
    candidate_id: candidate.id ?? null,
    job_id: job.id ?? null,
    score: round1(overall),
    eligible: blockers.length === 0,
    breakdown,
    blockers,
    gaps,
    warnings,
  };
}

function rank(pairs, { minScore = 0, limit = null, eligibleOnly = false }, idKey) {
  const kept = pairs.filter(
    (p) => p.result.score >= minScore && (p.result.eligible || !eligibleOnly)
  );
  kept.sort((a, b) => {
    if (a.result.eligible !== b.result.eligible) return a.result.eligible ? -1 : 1;
    if (a.result.score !== b.result.score) return b.result.score - a.result.score;
    if (a.label !== b.label) return a.label < b.label ? -1 : 1;
    const ia = String(a.result[idKey]);
    const ib = String(b.result[idKey]);
    return ia < ib ? -1 : ia > ib ? 1 : 0;
  });
  const results = kept.map((p) => p.result);
  return limit === null ? results : results.slice(0, limit);
}

/** Rank candidates for a job: eligible first, then score, then name. */
function rankCandidates(candidates, job, options = {}) {
  const { weights, asOf } = options;
  const pairs = candidates.map((c) => ({
    label: String(c.name ?? '').toLowerCase(),
    result: scoreCandidate(c, job, { weights, asOf }),
  }));
  return rank(pairs, options, 'candidate_id');
}

/** Rank jobs for a candidate: eligible first, then score, then job title. */
function matchJobsForCandidate(candidate, jobs, options = {}) {
  const { weights, asOf } = options;
  const pairs = jobs.map((j) => ({
    label: String(j.title ?? '').toLowerCase(),
    result: scoreCandidate(candidate, j, { weights, asOf }),
  }));
  return rank(pairs, options, 'job_id');
}

module.exports = {
  DEFAULT_WEIGHTS,
  normalize,
  registerAliases,
  scoreCandidate,
  rankCandidates,
  matchJobsForCandidate,
};
