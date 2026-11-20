// Pieces used on several screens.
import { h, input } from './dom.js';
import { fmtDate, expiryStatus } from './format.js';
import { get } from './api.js';

/** A licence as a ticket: name, expiry date, and where it stands today. */
export function ticket({ name, expiry }, actions = null) {
  const status = expiryStatus(expiry);
  return h('article', { class: `ticket is-${status.state}` },
    h('div', { class: 'ticket-stripe', 'aria-hidden': 'true' }),
    h('div', { class: 'ticket-body' },
      h('h3', { class: 'ticket-name' }, name),
      expiry
        ? h('p', { class: 'ticket-expiry' }, 'Expires', h('strong', {}, fmtDate(expiry)))
        : h('p', { class: 'ticket-expiry' }, h('strong', {}, 'No expiry')),
      h('p', { class: `status status-${status.state}` }, status.text),
      actions ? h('div', { class: 'actions' }, actions) : null));
}

export function score(result) {
  const value = Math.round(result.score);
  return h('span', {
    class: `score${result.eligible ? ' eligible' : ''}`,
    role: 'img',
    'aria-label': `Match score ${value} out of 100${result.eligible ? ', meets all requirements' : ''}`,
    vars: { '--score': String(value) },
  }, h('span', { class: 'score-num' }, value), h('span', { class: 'score-bar' }));
}

/** Why someone does or doesn't fit: blockers (can't start), gaps (could be better), warnings. */
export function reasons(result, { forCandidate = false } = {}) {
  const parts = [];
  if (result.eligible) {
    parts.push(h('span', { class: 'badge ok' }, forCandidate ? 'You meet the requirements' : 'Meets the requirements'));
  } else {
    parts.push(h('span', { class: 'badge bad' }, 'Missing a requirement'));
    parts.push(h('ul', { class: 'reasons bad' }, result.blockers.map((b) => h('li', {}, b))));
  }
  const notes = [...(result.warnings || []), ...(result.gaps || [])];
  if (notes.length) parts.push(h('ul', { class: 'reasons warn' }, notes.map((g) => h('li', {}, g))));
  return parts;
}

const catalogCache = {};

/** Suggestions for licence, qualification or skill names. */
export async function catalog(kind) {
  if (!catalogCache[kind]) catalogCache[kind] = get(`/catalog/${kind}`).catch(() => []);
  return catalogCache[kind];
}

let rulesCache = null;

/** Usual renewal cycles and notes for common licences, as a Map keyed by lower-case name. */
export async function licenceRules() {
  if (!rulesCache) {
    rulesCache = get('/catalog/licence-rules')
      .then((rows) => new Map(rows.map((r) => [r.name.toLowerCase(), r])))
      .catch(() => new Map());
  }
  return rulesCache;
}

let listCount = 0;

/**
 * Edit a list of names (e.g. required licences) as chips. withYears adds a "min years" box
 * (for skills). Returns { el, value() }.
 */
export function listEditor(label, items = [], { kind, withYears = false, hint } = {}) {
  const id = `list${++listCount}`;
  let values = items.map((item) => (typeof item === 'string' ? { name: item, min_years: null } : { ...item }));
  const chips = h('ul', { class: 'chips', 'aria-label': `Added: ${label.toLowerCase()}` });
  const name = input({ id: `${id}-name`, list: kind ? `${id}-options` : null, autocomplete: 'off' });
  const yearsBox = withYears ? input({ type: 'number', min: '0', max: '60', step: '0.5', inputmode: 'decimal', 'aria-label': 'Minimum years', placeholder: 'Min yrs' }) : null;
  const options = kind ? h('datalist', { id: `${id}-options` }) : null;
  if (kind) catalog(kind).then((names) => options.replaceChildren(...names.map((n) => h('option', { value: n }))));

  const draw = () => {
    chips.replaceChildren(...values.map((v, i) => h('li', { class: 'chip' },
      v.min_years ? `${v.name} (${v.min_years}+ yrs)` : v.name,
      h('button', { type: 'button', 'aria-label': `Remove ${v.name}`, onclick: () => { values.splice(i, 1); draw(); } }, '×'))));
  };
  const add = () => {
    const n = name.value.trim();
    if (!n) return;
    const minYears = yearsBox && yearsBox.value !== '' ? Number(yearsBox.value) : null;
    if (!values.some((v) => v.name.toLowerCase() === n.toLowerCase())) values.push({ name: n, min_years: minYears });
    name.value = '';
    if (yearsBox) yearsBox.value = '';
    draw();
    name.focus();
  };
  name.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } });
  if (yearsBox) yearsBox.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } });
  draw();

  const el = h('div', { class: 'field' },
    h('label', { for: `${id}-name` }, h('span', {}, label)),
    hint ? h('span', { class: 'hint' }, hint) : null,
    chips,
    h('div', { class: 'actions' },
      h('div', { class: 'grow' }, name), yearsBox ? h('div', { class: 'years' }, yearsBox) : null, options,
      h('button', { type: 'button', class: 'btn small', onclick: add }, 'Add')));
  return {
    el,
    value() {
      if (name.value.trim()) add(); // something typed but not added yet counts
      return values.map((v) => (withYears ? (v.min_years ? { name: v.name, min_years: v.min_years } : v.name) : v.name));
    },
  };
}

export function loading() {
  return h('p', { class: 'loading' }, 'Loading…');
}

export function pageHead(title, ...actions) {
  return h('div', { class: 'page-head' }, h('h1', {}, title), actions.length ? h('div', { class: 'actions' }, actions) : null);
}
