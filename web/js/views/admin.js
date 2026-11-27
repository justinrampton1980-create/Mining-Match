// Admin: placement terms, accounts, documents to check, activity log, inviting admins.
import { h, field, input, select, errorArea, onSubmit, actionButton, confirmButton, toast } from '../dom.js';
import { get, post, put, download, query } from '../api.js';
import { fmtDate, money, isoDate, today } from '../format.js';
import { statusBadge } from './invoice.js';

const TABS = [['', 'Placement terms'], ['invoices', 'Invoices'], ['users', 'Accounts'], ['documents', 'Documents'],
  ['reminders', 'Reminders'], ['activity', 'Activity'], ['admins', 'Admins']];

export async function admin(ctx) {
  const tab = ctx.params.tab || '';
  const view = { '': termsTab, invoices: invoicesTab, users: usersTab, documents: documentsTab, reminders: remindersTab, activity: activityTab, admins: adminsTab }[tab];
  if (!view) { ctx.go('/admin'); return h('div'); }
  return h('div', {},
    h('h1', {}, 'Admin'),
    h('nav', { class: 'tabs-inline', 'aria-label': 'Admin sections' }, TABS.map(([t, label]) =>
      h('a', { href: t ? `#/admin/${t}` : '#/admin', 'aria-current': t === tab ? 'page' : null }, label))),
    await view(ctx));
}

async function termsTab(ctx) {
  let current = null;
  try { current = await get('/fee-terms/current'); } catch (err) { if (err.status !== 404) throw err; }
  const percent = input({ type: 'number', min: '0.1', max: '100', step: '0.1', required: true, value: current ? current.fee_percent : '12' });
  const minFee = input({ type: 'number', min: '0', step: '1', value: current?.min_fee_aud ?? '' });
  const basis = input({ required: true, value: current ? current.fee_basis : 'Base salary for the first year, excluding allowances' });
  const windowMonths = input({ type: 'number', min: '1', max: '60', step: '1', required: true, value: current ? current.window_months : '12' });
  const guarantee = input({ type: 'number', min: '0', max: '730', step: '1', required: true, value: current ? current.guarantee_days : '90' });
  const text = h('textarea', { required: true, rows: '10', maxlength: '100000' }, current ? current.terms_text : '');
  const next = current ? current.version + 1 : 1;
  const error = errorArea();
  const publish = () => onSubmitPublish();
  async function onSubmitPublish() {
    error.clear();
    const body = {
      fee_percent: Number(percent.value), min_fee_aud: minFee.value === '' ? null : Number(minFee.value),
      fee_basis: basis.value, window_months: Number(windowMonths.value), guarantee_days: Number(guarantee.value),
      terms_text: text.value,
    };
    try {
      await post('/fee-terms', body);
    } catch (err) { error.show(err); return; }
    toast(`Version ${next} published.`);
    ctx.reload('h1');
  }
  return h('div', { class: 'stack' },
    current
      ? h('section', { class: 'panel stack' }, h('h2', {}, `Current: version ${current.version}`),
        h('p', {}, `${current.fee_percent}% of ${current.fee_basis.toLowerCase()}${current.min_fee_aud ? `, at least $${current.min_fee_aud}` : ''}. `
          + `${current.window_months}-month window, ${current.guarantee_days}-day guarantee. Published ${fmtDate(current.published_at)}.`),
        h('div', { class: 'terms-text' }, current.terms_text))
      : h('p', { class: 'notice warn' }, 'No placement terms published. Employers can’t unlock candidates until you publish them.'),
    h('section', { class: 'panel stack' },
      h('h2', {}, `Publish version ${next}`),
      h('p', { class: 'notice' }, 'Published terms can’t be edited or deleted. Employers must accept a new version before unlocking more candidates. Have a lawyer write the wording.'),
      h('div', { class: 'grid-2' }, field('Fee (% of salary)', percent), field('Minimum fee in AUD (optional)', minFee)),
      field('What the percentage applies to', basis),
      h('div', { class: 'grid-2' }, field('Fee window (months after unlocking)', windowMonths), field('Replacement guarantee (days)', guarantee)),
      field('Full terms', text), error.el,
      h('div', { class: 'actions' }, confirmButton(`Publish version ${next}`, 'Tap again: this can’t be changed later', publish, { class: 'btn primary' }))));
}

async function invoicesTab(ctx) {
  const settings = await get('/billing/settings');
  if (!settings.enabled) {
    return h('p', { class: 'notice warn' }, 'Invoicing isn’t set up. Set MM_ABN, MM_BUSINESS_NAME and the bank details on the server (see the README), then restart it.');
  }
  const status = ctx.query.status || '';
  const [list, all, employers] = await Promise.all([
    get(`/invoices${query({ status })}`), status ? get('/invoices') : null, get('/admin/users?role=employer&limit=500')]);
  const everything = all || list;
  const now = isoDate(today());
  const sum = (items) => items.reduce((n, i) => n + i.total_cents, 0);
  const outstanding = everything.filter((i) => i.display_status === 'due' || i.display_status === 'overdue');
  const overdue = everything.filter((i) => i.display_status === 'overdue');
  const ready = everything.filter((i) => i.status === 'scheduled' && i.issue_date <= now);

  const filter = select([['', 'All invoices'], ['scheduled', 'Scheduled'], ['due', 'Due'], ['overdue', 'Overdue'], ['paid', 'Paid'], ['void', 'Void']],
    status, { 'aria-label': 'Show' });
  filter.addEventListener('change', () => ctx.go(`/admin/invoices${query({ status: filter.value })}`));

  // New invoice for other services (e.g. a subscription), issued straight away
  const employer = select([['', 'Choose an employer'], ...employers.filter((u) => u.employer_id).map((u) => [u.employer_id, u.company_name])], '');
  const lines = h('div', { class: 'stack' });
  const addLine = () => {
    if (lines.children.length >= 10) return;
    const n = lines.children.length + 1;
    lines.append(h('div', { class: 'grid-2 line' },
      field(`Line ${n}: description`, input({ maxlength: '200', class: 'desc' })),
      field(`Line ${n}: amount excl. GST (AUD)`, input({ inputmode: 'decimal', class: 'amount' }))));
  };
  addLine();
  const dueDays = input({ type: 'number', min: '1', max: '90', value: String(settings.payment_terms_days) });
  const error = errorArea();
  const newForm = onSubmit(h('form', { class: 'panel stack' }, h('h2', {}, 'New invoice'),
    h('p', { class: 'small muted' }, 'For services other than placements, such as a subscription. It’s issued and emailed straight away.'),
    field('Employer', employer), lines,
    h('div', { class: 'actions' }, h('button', { type: 'button', class: 'btn small', onclick: addLine }, 'Add a line')),
    field('Due in (days)', dueDays), error.el,
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary' }, 'Issue invoice'))), error, async () => {
    if (!employer.value) throw new Error('Choose an employer.');
    const items = [...lines.querySelectorAll('.line')].map((row) => ({
      description: row.querySelector('.desc').value.trim(),
      amount: row.querySelector('.amount').value.replace(/[$,\s]/g, ''),
    })).filter((l) => l.description || l.amount);
    if (!items.length) throw new Error('Add at least one line.');
    const created = await post('/invoices', { employer_id: employer.value, lines: items, due_days: Number(dueDays.value) });
    toast(`${created.number} issued and emailed.`);
    ctx.go(`/invoices/${created.id}`);
  });

  const seller = settings.seller; const pay = settings.payment;
  return h('div', { class: 'stack' },
    h('div', { class: 'grid-2' },
      h('div', { class: 'panel' }, h('p', { class: 'muted small' }, 'Waiting for payment'), h('p', { class: 'total-figure' }, money(sum(outstanding)))),
      h('div', { class: 'panel' }, h('p', { class: 'muted small' }, 'Overdue'), h('p', { class: `total-figure${overdue.length ? ' bad-text' : ''}` }, money(sum(overdue))))),
    ready.length ? h('div', { class: 'notice warn actions' },
      h('span', {}, `${ready.length} scheduled invoice${ready.length === 1 ? ' is' : 's are'} ready to issue. The daily job normally does this.`),
      actionButton('Issue them now', async () => {
        const result = await post('/invoices/issue-due');
        toast(result.issued.length ? `Issued ${result.issued.join(', ')}.` : 'Nothing to issue.');
        ctx.reload('h1');
      }, { class: 'btn small' })) : null,
    h('div', { class: 'actions' }, field('Show', filter)),
    list.length ? h('div', { class: 'scroll-x' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, ['Invoice', 'Employer', 'Issued', 'Total', 'Status'].map((t) => h('th', { scope: 'col' }, t)))),
      h('tbody', {}, list.map((i) => h('tr', {},
        h('td', {}, h('a', { href: `#/invoices/${i.id}` }, i.number || 'Scheduled')),
        h('td', {}, i.company_name), h('td', {}, fmtDate(i.issue_date)),
        h('td', {}, money(i.total_cents)), h('td', {}, statusBadge(i)))))))
      : h('p', { class: 'empty' }, 'No invoices here.'),
    newForm,
    h('section', { class: 'panel stack' }, h('h2', {}, 'Invoices are issued as'),
      h('p', {}, `${seller.legal_name}, ABN ${seller.abn}. ${seller.gst_registered ? 'Registered for GST: 10% is added.' : 'Not registered for GST: no GST is charged.'}`),
      h('p', {}, `Paid to ${pay.account_name}, BSB ${pay.bsb}, account ${pay.account_number}. Due ${settings.payment_terms_days} days after issue.`),
      h('p', { class: 'small muted' }, 'These come from the server settings, not from this website, so nobody can change where employers pay by getting into an account.')));
}

async function usersTab(ctx) {
  const search = input({ type: 'search', value: ctx.query.q || '', placeholder: 'Email, name or company' });
  const role = select([['', 'All accounts'], ['candidate', 'Candidates'], ['employer', 'Employers'], ['admin', 'Admins']], ctx.query.role || '');
  const users = await get(`/admin/users${query({ search: ctx.query.q, role: ctx.query.role })}`);
  const error = errorArea();
  const form = onSubmit(h('form', { class: 'actions', role: 'search' },
    h('div', { class: 'grow' }, field('Find', search)), field('Type', role),
    h('button', { type: 'submit', class: 'btn' }, 'Search')), error, async () => {
    ctx.go(`/admin/users${query({ q: search.value, role: role.value })}`);
  });
  return h('div', { class: 'stack' }, form, error.el,
    users.length ? h('ul', { class: 'rows' }, users.map((u) => {
      const reason = input({ placeholder: 'Reason (no personal details)', 'aria-label': `Reason for suspending ${u.email}`, maxlength: '200' });
      return h('li', { class: 'row' },
        h('div', { class: 'row-main' }, h('h3', {}, u.candidate_name || u.company_name || u.email),
          h('span', { class: 'actions' },
            h('span', { class: 'badge' }, u.role),
            u.closed ? h('span', { class: 'badge' }, 'Closed') : u.is_active ? null : h('span', { class: 'badge bad' }, 'Suspended'),
            u.email_verified ? null : h('span', { class: 'badge warn' }, 'Email not confirmed'))),
        h('p', { class: 'meta' }, h('span', {}, u.email), h('span', {}, `Joined ${fmtDate(u.created_at)}`)),
        h('div', { class: 'actions' }, actionButton('Export data', () => download(`/admin/users/${u.user_id}/export`,
          `mining-match-user-data-${u.user_id.slice(0, 8)}.zip`), { class: 'btn small', 'aria-label': `Export all data held about ${u.email}` })),
        u.role === 'employer' && !u.closed ? h('div', { class: 'actions' },
          confirmButton('Close account', 'Tap again: jobs close, sign-in is deleted', async () => {
            await post(`/employers/${u.employer_id}/close`, { reason: 'closed on request' });
            toast('Account closed. Invoices and hires are kept.');
            ctx.reload('h1');
          }, { class: 'btn small danger' })) : null,
        u.closed ? null : u.user_id === ctx.me.user_id ? h('p', { class: 'small muted' }, 'This is you.') : u.is_active
          ? h('div', { class: 'actions' }, h('div', { class: 'grow' }, reason),
            confirmButton('Suspend', 'Tap again to suspend', async () => {
              await put(`/admin/users/${u.user_id}/active`, { active: false, reason: reason.value || null });
              toast('Account suspended and logged out.');
              ctx.reload('h1');
            }, { class: 'btn danger small' }))
          : h('div', { class: 'actions' }, actionButton('Reactivate', async () => {
            await put(`/admin/users/${u.user_id}/active`, { active: true });
            toast('Account reactivated.');
            ctx.reload('h1');
          }, { class: 'btn small' })));
    })) : h('p', { class: 'empty' }, 'No accounts found.'));
}

async function documentsTab(ctx) {
  const docs = await get('/admin/documents');
  return h('div', { class: 'stack' },
    h('p', { class: 'lede' }, 'Uploaded documents nobody has checked yet, oldest first. Check each against the issuing register where one exists before marking it checked.'),
    docs.length ? h('ul', { class: 'rows' }, docs.map((d) => h('li', { class: 'row' },
      h('div', { class: 'row-main' }, h('h3', {}, d.filename), h('span', { class: 'badge' }, d.kind)),
      h('p', { class: 'meta' }, h('span', {}, d.candidate_name), h('span', {}, `Uploaded ${fmtDate(d.uploaded_at)}`)),
      h('div', { class: 'actions' },
        actionButton('Download', () => download(`/documents/${d.id}/file`, d.filename), { class: 'btn small' }),
        actionButton('Mark as checked', async () => {
          await post(`/documents/${d.id}/verify`);
          toast('Marked as checked.');
          ctx.reload('h1');
        }, { class: 'btn primary small' })))))
      : h('p', { class: 'empty' }, 'Nothing waiting to be checked.'));
}

const STAGE_WORDS = { 90: '90 days', 60: '60 days', 30: '30 days', 0: 'Expired' };

async function remindersTab(ctx) {
  const [sent, rules] = await Promise.all([get('/admin/reminders?limit=200'), get('/catalog/licence-rules')]);
  return h('div', { class: 'stack' },
    h('p', { class: 'lede' }, 'Candidates get an email 90, 60 and 30 days before each licence expires, and once if one lapses. '
      + 'The daily job sends them; this is here for when it hasn’t run.'),
    h('div', { class: 'actions' }, actionButton('Send today’s reminders now', async () => {
      const result = await post('/admin/reminders/run');
      toast(result.emails
        ? `Sent ${result.emails} email${result.emails === 1 ? '' : 's'} about ${result.licences} licence${result.licences === 1 ? '' : 's'}.`
        : 'Nothing due today.');
      ctx.reload('h1');
    }, { class: 'btn primary' })),
    h('section', { 'aria-labelledby': 'sent-title' },
      h('h2', { id: 'sent-title' }, 'Reminders sent'),
      sent.length ? h('div', { class: 'scroll-x' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['Sent', 'Candidate', 'Licence', 'Expires', 'Reminder'].map((t) => h('th', { scope: 'col' }, t)))),
        h('tbody', {}, sent.map((r) => h('tr', {},
          h('td', {}, fmtDate(r.sent_on)), h('td', {}, r.candidate_name), h('td', {}, r.licence),
          h('td', {}, fmtDate(r.expiry_date)),
          h('td', {}, h('span', { class: `badge${r.stage === 0 ? ' bad' : ''}` }, STAGE_WORDS[r.stage] || r.stage)))))))
        : h('p', { class: 'empty' }, 'No reminders sent yet.')),
    h('section', { 'aria-labelledby': 'rules-title' },
      h('h2', { id: 'rules-title' }, 'Renewal cycles'),
      h('p', { class: 'small muted' }, 'Used to suggest an expiry date when a candidate adds a licence. Edit them in db/catalog_seed.json.'),
      h('ul', { class: 'rows' }, rules.map((r) => h('li', { class: 'row' },
        h('div', { class: 'row-main' }, h('h3', {}, r.name),
          h('span', { class: 'badge' }, r.renew_years ? `Every ${r.renew_years} yrs` : 'No set cycle')),
        r.note ? h('p', { class: 'small muted' }, r.note) : null)))));
}

async function activityTab(ctx) {
  const outcome = ctx.query.outcome || '';
  const rows = await get(`/admin/audit-log${query({ outcome, limit: 200 })}`);
  const filter = select([['', 'Everything'], ['denied', 'Refused only'], ['allowed', 'Allowed only']], outcome, { 'aria-label': 'Show' });
  filter.addEventListener('change', () => ctx.go(`/admin/activity${query({ outcome: filter.value })}`));
  return h('div', { class: 'stack' },
    h('div', { class: 'actions' }, field('Show', filter)),
    h('div', { class: 'scroll-x' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, ['When (UTC)', 'Who', 'Action', 'Result', 'Record', 'Detail'].map((t) => h('th', { scope: 'col' }, t)))),
      h('tbody', {}, rows.map((r) => h('tr', {},
        h('td', {}, r.at), h('td', {}, r.role || ''), h('td', {}, r.action),
        h('td', {}, h('span', { class: `badge${r.outcome === 'denied' ? ' bad' : ''}` }, r.outcome)),
        h('td', {}, r.resource_type ? `${r.resource_type} ${String(r.resource_id || '').slice(0, 8)}` : ''),
        h('td', {}, r.detail || '')))))));
}

async function adminsTab(ctx) {
  const email = input({ type: 'email', required: true, autocomplete: 'off' });
  const error = errorArea();
  return onSubmit(h('form', { class: 'panel stack' },
    h('h2', {}, 'Invite an admin'),
    h('p', {}, 'They’ll get an email with a link to set their password. Admins can see every account and document.'),
    field('Email', email), error.el,
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary' }, 'Send invitation'))), error, async () => {
    await post('/admin/admins', { email: email.value });
    toast(`Invitation sent to ${email.value}.`);
    email.value = '';
  });
}
