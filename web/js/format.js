// Dates, expiry and wording shared across screens.

const DAY = 86400000;
const dateFormat = new Intl.DateTimeFormat('en-AU', { day: 'numeric', month: 'short', year: 'numeric' });

/** 'YYYY-MM-DD' (or a timestamp starting with it) as a local date, without time-zone shifts. */
export function parseDate(value) {
  if (!value) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value));
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}

export function fmtDate(value) {
  const d = parseDate(value);
  return d ? dateFormat.format(d) : '';
}

export function today() {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

export function isoDate(date) {
  const p = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}`;
}

/** 'YYYY-MM-DD' plus whole years, as 'YYYY-MM-DD'. 29 Feb becomes 28 Feb in a year without one. */
export function addYears(value, n) {
  const d = parseDate(value);
  if (!d) return '';
  const target = new Date(d.getFullYear() + n, d.getMonth(), d.getDate());
  if (target.getMonth() !== d.getMonth()) target.setDate(0); // rolled into March: back to 28 Feb
  return isoDate(target);
}

export const EXPIRY_WARNING_DAYS = 90; // same as the matching engine

/** Where a licence stands today: none | valid | expiring | expired, with words to show. */
export function expiryStatus(expiry) {
  const d = parseDate(expiry);
  if (!d) return { state: 'none', text: 'Doesn’t expire' };
  const days = Math.round((d - today()) / DAY);
  if (days < 0) return { state: 'expired', text: `Expired ${fmtDate(expiry)}` };
  if (days === 0) return { state: 'expiring', text: 'Expires today' };
  if (days <= EXPIRY_WARNING_DAYS) return { state: 'expiring', text: `Expires in ${days} day${days === 1 ? '' : 's'}` };
  return { state: 'valid', text: 'Valid' };
}

export const STATES = [
  ['', 'Not set'], ['QLD', 'Queensland'], ['NSW', 'New South Wales'], ['WA', 'Western Australia'],
  ['NT', 'Northern Territory'], ['SA', 'South Australia'], ['VIC', 'Victoria'], ['TAS', 'Tasmania'], ['ACT', 'ACT'],
];

export function stateName(code) {
  const found = STATES.find(([c]) => c === code);
  return found && code ? found[1] : code || '';
}

export const CONTACT_WORDS = {
  none: 'Contact details hidden',
  requested: 'Waiting for the candidate',
  declined: 'Candidate declined',
  approved: 'Candidate approved: unlock to see details',
  unlocked: 'Details unlocked',
};

export function years(n) {
  if (n === null || n === undefined) return '';
  const v = Math.round(n * 10) / 10;
  return `${v} yr${v === 1 ? '' : 's'}`;
}

const aud = new Intl.NumberFormat('en-AU', { style: 'currency', currency: 'AUD' });

/** Whole cents as dollars, e.g. 1584000 -> "$15,840.00". */
export function money(cents) {
  return aud.format((cents || 0) / 100);
}

export function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}
