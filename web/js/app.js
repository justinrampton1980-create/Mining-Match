// Mining Match web app: works out who is logged in, shows the right navigation and screen.
import { h, toast } from './dom.js';
import { get } from './api.js';
import { route, parse, go, replace } from './router.js';
import { loading } from './components.js';
import * as authViews from './views/auth.js';
import * as accountViews from './views/account.js';
import * as candidateViews from './views/candidate.js';
import * as employerViews from './views/employer.js';
import * as adminViews from './views/admin.js';
import * as invoiceViews from './views/invoice.js';

const HOME = { candidate: '/profile', employer: '/jobs', admin: '/admin' };
const NAV = {
  candidate: [['/profile', 'Profile'], ['/matches', 'Jobs'], ['/requests', 'Requests'], ['/account', 'Account']],
  employer: [['/jobs', 'Jobs'], ['/search', 'Search'], ['/shortlists', 'Shortlists'], ['/billing', 'Billing'], ['/account', 'Account']],
  admin: [['/admin', 'Admin'], ['/account', 'Account']],
};
const ALL = ['candidate', 'employer', 'admin'];

const state = { me: null };

const ctx = {
  get me() { return state.me; },
  home() { return state.me ? HOME[state.me.role] : '/login'; },
  async refresh() {
    try { state.me = await get('/me'); } catch { state.me = null; }
    drawNav(parse().path);
  },
  go,
  replace,
  /** Redraw the current screen in place (after a change), keeping the scroll position. */
  reload(focusSelector) { return render({ soft: true, focus: focusSelector }); },
};

// ------------------------------------------------------------------ routes
route('/login', authViews.login, { public: true, title: 'Log in' });
route('/signup', authViews.signup, { public: true, title: 'Create an account' });
route('/check-email', authViews.checkEmail, { public: true, title: 'Check your email' });
route('/verify-email', authViews.verifyEmail, { public: true, anyone: true, title: 'Confirm your email' });
route('/unsubscribe', authViews.unsubscribe, { public: true, anyone: true, title: 'Licence reminders' });
route('/forgot-password', authViews.forgot, { public: true, title: 'Reset your password' });
route('/reset-password', authViews.reset, { public: true, anyone: true, title: 'Choose a new password' });
route('/account', accountViews.account, { roles: ALL, title: 'Account' });

route('/profile', candidateViews.profile, { roles: ['candidate'], title: 'Your profile' });
route('/matches', candidateViews.matches, { roles: ['candidate'], title: 'Jobs for you' });
route('/matches/:id', candidateViews.job, { roles: ['candidate'], title: 'Job' });
route('/requests', candidateViews.requests, { roles: ['candidate'], title: 'Requests' });

route('/jobs', employerViews.jobs, { roles: ['employer'], title: 'Jobs' });
route('/jobs/new', employerViews.jobForm, { roles: ['employer'], title: 'Post a job' });
route('/jobs/:id', employerViews.jobCandidates, { roles: ['employer'], title: 'Candidates' });
route('/jobs/:id/edit', employerViews.jobForm, { roles: ['employer'], title: 'Edit job' });
route('/candidates/:id', employerViews.candidate, { roles: ['employer'], title: 'Candidate' });
route('/search', employerViews.search, { roles: ['employer'], title: 'Search candidates' });
route('/shortlists', employerViews.shortlists, { roles: ['employer'], title: 'Shortlists' });
route('/shortlists/:id', employerViews.shortlist, { roles: ['employer'], title: 'Shortlist' });
route('/billing', employerViews.billing, { roles: ['employer'], title: 'Billing' });
route('/fees', employerViews.billing, { roles: ['employer'], title: 'Billing' });
route('/hire/:id', employerViews.hire, { roles: ['employer'], title: 'Record a hire' });
route('/invoices/:id', invoiceViews.invoice, { roles: ['employer', 'admin'], title: 'Invoice' });

route('/admin', adminViews.admin, { roles: ['admin'], title: 'Admin' });
route('/admin/:tab', adminViews.admin, { roles: ['admin'], title: 'Admin' });

// -------------------------------------------------------------- navigation
function drawNav(path) {
  const nav = document.getElementById('nav');
  const items = state.me ? NAV[state.me.role] : [];
  nav.replaceChildren(...items.map(([href, label]) => {
    const current = path === href || path.startsWith(`${href}/`);
    return h('a', { href: `#${href}`, 'aria-current': current ? 'page' : null }, label);
  }));
}

// ------------------------------------------------------------------ render
let renderCount = 0;

async function render({ soft = false, focus = null } = {}) {
  if (location.hash && !location.hash.startsWith('#/')) return; // e.g. the skip link
  const { route: r, path, params, query } = parse();
  const me = state.me;
  if (!r) return go(ctx.home());
  if (r.public && me && !r.anyone) return go(HOME[me.role]);
  if (!r.public && !me) return go('/login');
  if (r.roles && !r.roles.includes(me.role)) return go(HOME[me.role]);

  const id = ++renderCount;
  const main = document.getElementById('main');
  const scroll = window.scrollY;
  if (!soft) main.replaceChildren(loading());
  drawNav(path);
  try {
    const view = await r.view({ ...ctx, params, query });
    if (id !== renderCount) return; // the person has already moved on
    main.replaceChildren(view);
    const heading = main.querySelector('h1');
    document.title = `${heading ? heading.textContent : r.title} | Mining Match`;
    if (soft) {
      window.scrollTo(0, scroll);
      const target = focus && main.querySelector(focus);
      if (target) { target.tabIndex = -1; target.focus({ preventScroll: true }); }
    } else {
      window.scrollTo(0, 0);
      if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
    }
  } catch (err) {
    if (id !== renderCount) return;
    if (err.status === 401) return sessionEnded();
    main.replaceChildren(h('div', { class: 'stack' },
      h('h1', {}, err.status === 404 ? 'Not found' : err.status === 403 ? 'Not available' : 'Something went wrong'),
      h('p', {}, err.message),
      h('p', {}, h('a', { href: `#${ctx.home()}` }, 'Go to your home page'))));
  }
}

function sessionEnded() {
  const wasLoggedIn = Boolean(state.me);
  state.me = null;
  drawNav('');
  if (wasLoggedIn) toast('You’ve been logged out. Log in again to continue.');
  go('/login');
}

window.addEventListener('mm:logged-out', sessionEnded);
window.addEventListener('hashchange', () => render());

await ctx.refresh();
render();
