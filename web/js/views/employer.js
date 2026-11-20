// Employer screens: jobs, ranked candidates, candidate profiles with the two-step unlock,
// search, shortlists and placement fees.
import { h, field, input, select, checkbox, errorArea, onSubmit, actionButton, confirmButton, toast } from '../dom.js';
import { get, post, put, del, download, query } from '../api.js';
import { statusBadge as invoiceBadge, payBox } from './invoice.js';
import { ticket, score, reasons, listEditor, pageHead } from '../components.js';
import { fmtDate, years, STATES, stateName, CONTACT_WORDS, plural, money } from '../format.js';

const JOB_STATUS = [['draft', 'Draft'], ['open', 'Open'], ['filled', 'Filled'], ['closed', 'Closed']];
const SHORTLIST_STATUS = [['shortlisted', 'Shortlisted'], ['contacted', 'Contacted'], ['interviewing', 'Interviewing'],
  ['offered', 'Offered'], ['placed', 'Placed'], ['rejected', 'Not going ahead']];

function statusBadge(status) {
  const label = (JOB_STATUS.find(([v]) => v === status) || [status, status])[1];
  return h('span', { class: `badge${status === 'open' ? ' ok' : ''}` }, label);
}

function requirementCount(j) {
  const n = (j.required_licences || []).length + (j.required_qualifications || []).length + (j.required_skills || []).length;
  return n ? plural(n, 'requirement') : 'No requirements yet';
}

function candidateRow(r, jobId, extra = null) {
  const href = `#/candidates/${r.candidate_id}${jobId ? `?job=${jobId}` : ''}`;
  return h('li', { class: 'row' },
    h('div', { class: 'row-main' }, h('h3', {}, h('a', { href }, r.display_name)), score(r)),
    reasons(r),
    h('p', { class: 'meta' }, h('span', {}, CONTACT_WORDS[r.contact_status] || '')),
    h('div', { class: 'actions' }, h('a', { class: 'btn small', href }, 'View profile'), extra));
}

// ---------------------------------------------------------------------- jobs
export async function jobs() {
  const list = await get('/jobs');
  const post_ = h('a', { class: 'btn primary', href: '#/jobs/new' }, 'Post a job');
  return h('div', {},
    pageHead('Jobs', post_),
    list.length
      ? h('ul', { class: 'rows' }, list.map((j) => h('li', { class: 'row' },
        h('div', { class: 'row-main' }, h('h3', {}, h('a', { href: `#/jobs/${j.id}` }, j.title)), statusBadge(j.status)),
        h('p', { class: 'meta' },
          j.location ? h('span', {}, stateName(j.location)) : null,
          j.start_date ? h('span', {}, `Starts ${fmtDate(j.start_date)}`) : null,
          h('span', {}, requirementCount(j))),
        h('div', { class: 'actions' },
          h('a', { class: 'btn small', href: `#/jobs/${j.id}` }, 'See candidates'),
          h('a', { class: 'btn small quiet', href: `#/jobs/${j.id}/edit` }, 'Edit')))))
      : h('div', { class: 'panel stack' },
        h('p', {}, 'Post a job to see candidates ranked against it, best match first. Candidates who can’t start, for example because a licence expires before your start date, are flagged.'),
        h('div', { class: 'actions' }, h('a', { class: 'btn primary', href: '#/jobs/new' }, 'Post a job'))));
}

export async function jobForm(ctx) {
  const id = ctx.params.id;
  const j = id ? await get(`/jobs/${id}`) : {};
  const title = input({ value: j.title || '', required: true, maxlength: '120' });
  const description = h('textarea', { maxlength: '10000' }, j.description || '');
  const state = select(STATES, j.location || '');
  const start = input({ type: 'date', value: j.start_date || '' });
  const minExp = input({ type: 'number', min: '0', max: '60', step: '0.5', inputmode: 'decimal', value: j.min_experience_years ?? '' });
  const reqLic = listEditor('Licences they must hold', j.required_licences || [], { kind: 'licence',
    hint: 'Candidates missing one of these, or whose licence expires before the start date, are marked as unable to start.' });
  const prefLic = listEditor('Licences that help', j.preferred_licences || [], { kind: 'licence' });
  const reqQual = listEditor('Qualifications they must hold', j.required_qualifications || [], { kind: 'qualification' });
  const prefQual = listEditor('Qualifications that help', j.preferred_qualifications || [], { kind: 'qualification' });
  const reqSkill = listEditor('Skills needed', j.required_skills || [], { kind: 'skill', withYears: true,
    hint: 'Add a minimum number of years where it matters.' });
  const prefSkill = listEditor('Skills that help', j.preferred_skills || [], { kind: 'skill' });
  const roles = listEditor('Roles that count as experience', j.relevant_roles || [], {
    hint: 'Years in these roles count toward the minimum experience. Leave empty to count all experience.' });
  const status = select(JOB_STATUS, j.status || 'draft');
  const error = errorArea();
  const form = onSubmit(h('form', { class: 'stack' },
    h('section', { class: 'panel stack' }, h('h2', {}, 'The job'),
      field('Job title', title),
      h('div', { class: 'grid-2' }, field('State', state), field('Start date', start, 'Licences are checked against this date.')),
      field('Description (optional)', description)),
    h('section', { class: 'panel stack' }, h('h2', {}, 'Requirements'),
      reqLic.el, prefLic.el, reqQual.el, prefQual.el, reqSkill.el, prefSkill.el,
      field('Minimum years of experience (optional)', minExp), roles.el),
    h('section', { class: 'panel stack' }, h('h2', {}, 'Visibility'),
      field('Status', status, 'Open jobs are shown to candidates. You can see ranked candidates for any job, including drafts.')),
    error.el,
    h('div', { class: 'actions' },
      h('button', { type: 'submit', class: 'btn primary' }, id ? 'Save changes' : 'Save job'),
      h('a', { class: 'btn quiet', href: id ? `#/jobs/${id}` : '#/jobs' }, 'Cancel'))), error, async () => {
    const body = {
      title: title.value, description: description.value || null, location: state.value || null,
      start_date: start.value || null, min_experience_years: minExp.value === '' ? null : Number(minExp.value),
      required_licences: reqLic.value(), preferred_licences: prefLic.value(),
      required_qualifications: reqQual.value(), preferred_qualifications: prefQual.value(),
      required_skills: reqSkill.value(), preferred_skills: prefSkill.value(), relevant_roles: roles.value(),
    };
    if (id) {
      await put(`/jobs/${id}`, body);
      if (status.value !== j.status) await put(`/jobs/${id}/status`, { status: status.value });
      toast('Job saved.');
      ctx.go(`/jobs/${id}`);
    } else {
      const created = await post('/jobs', { ...body, status: status.value });
      toast('Job saved.');
      ctx.go(`/jobs/${created.id}`);
    }
  });
  return h('div', {},
    h('a', { class: 'back', href: id ? `#/jobs/${id}` : '#/jobs' }, id ? 'Back to candidates' : 'Back to jobs'),
    pageHead(id ? `Edit: ${j.title}` : 'Post a job'), form);
}

export async function jobCandidates(ctx) {
  const id = ctx.params.id;
  const eligibleOnly = ctx.query.eligible === '1';
  const [j, ranked, lists] = await Promise.all([
    get(`/jobs/${id}`),
    get(`/jobs/${id}/ranked-candidates${query({ eligible_only: eligibleOnly ? 'true' : null })}`),
    get('/shortlists'),
  ]);
  let shortlistId = (lists.find((s) => s.job_id === id) || {}).id;
  const onList = new Set(shortlistId ? (await get(`/shortlists/${shortlistId}`)).entries.map((e) => e.candidate_id) : []);
  const addToShortlist = (candidateId) => async (button) => {
    if (!shortlistId) shortlistId = (await post('/shortlists', { name: `${j.title} shortlist`, job_id: id })).id;
    await post(`/shortlists/${shortlistId}/entries`, { candidate_id: candidateId });
    onList.add(candidateId);
    button.replaceWith(h('a', { class: 'small', href: `#/shortlists/${shortlistId}` }, 'On shortlist'));
    toast('Added to the shortlist.');
  };
  const statusSelect = select(JOB_STATUS, j.status, { id: 'job-status' });
  statusSelect.addEventListener('change', async () => {
    try {
      await put(`/jobs/${id}/status`, { status: statusSelect.value });
      toast(statusSelect.value === 'open' ? 'Job is open: candidates can see it.' : 'Status saved.');
    } catch (err) { toast(err.message, 'bad'); statusSelect.value = j.status; }
  });
  const filter = checkbox('Only show candidates who meet every requirement', eligibleOnly, {
    onchange: () => ctx.go(`/jobs/${id}${eligibleOnly ? '' : '?eligible=1'}`) });
  const ready = ranked.filter((r) => r.eligible).length;
  return h('div', {},
    h('a', { class: 'back', href: '#/jobs' }, 'All jobs'),
    pageHead(j.title, h('a', { class: 'btn small', href: `#/jobs/${id}/edit` }, 'Edit job')),
    h('p', { class: 'meta' },
      j.location ? h('span', {}, stateName(j.location)) : null,
      j.start_date ? h('span', {}, `Starts ${fmtDate(j.start_date)}`) : null,
      h('span', {}, requirementCount(j))),
    h('div', { class: 'inline-field' }, h('label', { for: 'job-status' }, 'Status'), statusSelect),
    h('section', { 'aria-labelledby': 'ranked' },
      h('h2', { id: 'ranked' }, 'Candidates'),
      h('p', { class: 'muted' }, eligibleOnly
        ? `${plural(ranked.length, 'candidate')} ${ranked.length === 1 ? 'meets' : 'meet'} every requirement.`
        : `${plural(ranked.length, 'candidate')}, best match first. ${ready} ${ready === 1 ? 'meets' : 'meet'} every requirement.`),
      filter.el,
      ranked.length
        ? h('ul', { class: 'rows' }, ranked.map((r) => candidateRow(r, id,
          onList.has(r.candidate_id)
            ? h('a', { class: 'small', href: `#/shortlists/${shortlistId}` }, 'On shortlist')
            : actionButton('Add to shortlist', addToShortlist(r.candidate_id), { class: 'btn small' }))))
        : h('p', { class: 'empty' }, eligibleOnly
          ? 'Nobody meets every requirement yet. Turn off the filter to see the closest matches.'
          : 'No candidates yet. They appear here as they add their licences and skills.')));
}

// ------------------------------------------------------------- one candidate
function termsSummary(t) {
  const basis = t.fee_basis.charAt(0).toLowerCase() + t.fee_basis.slice(1);
  return h('ul', { class: 'reasons' },
    h('li', {}, `${t.fee_percent}% of ${basis}${t.min_fee_aud ? `, at least $${Number(t.min_fee_aud).toLocaleString('en-AU')}` : ''}`),
    h('li', {}, `Payable if you hire them within ${t.window_months} months of unlocking`),
    h('li', {}, `${t.guarantee_days}-day replacement guarantee`));
}

function contactPanel(ctx, c, terms, jobId) {
  const reload = () => ctx.reload('#contact-title');
  const parts = [h('h2', { id: 'contact-title' }, 'Contact details')];
  if (c.contact_status === 'none') {
    parts.push(h('p', {}, 'Their name, phone, email and documents are hidden until they approve you.'),
      h('div', { class: 'actions' }, actionButton('Ask to see contact details', async () => {
        await post(`/candidates/${c.id}/contact-request`, { job_id: jobId });
        toast('Request sent.');
        reload();
      }, { class: 'btn primary' })));
  } else if (c.contact_status === 'requested') {
    parts.push(h('p', {}, 'Request sent. Once they approve, you can unlock their details here.'));
  } else if (c.contact_status === 'declined') {
    parts.push(h('p', {}, 'This candidate chose not to share their details.'));
  } else if (c.contact_status === 'approved' && !terms) {
    parts.push(h('p', {}, 'They approved you. Unlocking needs Mining Match’s placement terms, which haven’t been published yet. Contact Mining Match.'));
  } else if (c.contact_status === 'approved') {
    const agree = terms.accepted ? null : checkbox(`I accept the placement terms (version ${terms.version})`, false);
    const error = errorArea();
    parts.push(
      h('p', {}, 'They approved you. Unlocking their details is an introduction under Mining Match’s placement terms:'),
      termsSummary(terms),
      h('details', {}, h('summary', {}, 'Read the full terms'), h('div', { class: 'terms-text' }, terms.terms_text)),
      onSubmit(h('form', { class: 'stack' }, agree ? agree.el : h('p', { class: 'small muted' }, `You accepted version ${terms.version}.`),
        error.el, h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary' }, 'Unlock contact details'))),
      error, async () => {
        if (agree && !agree.box.checked) throw new Error('Tick the box to accept the placement terms first.');
        if (agree) await post(`/fee-terms/${terms.version}/accept`);
        await post(`/candidates/${c.id}/unlock`, { terms_version: terms.version, job_id: jobId });
        toast('Details unlocked.');
        reload();
      }));
  } else if (c.contact_status === 'unlocked') {
    parts.push(
      h('span', { class: 'badge ok' }, 'Unlocked'),
      h('ul', { class: 'stack bare-list' },
        c.email ? h('li', {}, h('a', { href: `mailto:${c.email}` }, c.email)) : null,
        c.phone ? h('li', {}, h('a', { href: `tel:${c.phone.replace(/[^\d+]/g, '')}` }, c.phone)) : null),
      c.summary ? h('p', { class: 'pre' }, c.summary) : null,
      h('p', { class: 'small muted' }, 'Hiring this person within the fee window means a placement fee applies.'),
      h('div', { class: 'actions' }, h('a', { class: 'btn', href: `#/hire/${c.id}${jobId ? `?job=${jobId}` : ''}` }, 'Record a hire')));
  }
  return h('aside', { class: 'panel stack', 'aria-labelledby': 'contact-title' }, parts);
}

export async function candidate(ctx) {
  const id = ctx.params.id;
  const jobId = ctx.query.job || null;
  const c = await get(`/candidates/${id}`);
  let terms = null;
  if (c.contact_status === 'approved') {
    try { terms = await get('/fee-terms/current'); } catch (err) { if (err.status !== 404) throw err; }
  }
  const unlocked = c.contact_status === 'unlocked';
  const flags = [c.willing_to_relocate ? 'Will relocate' : null, c.willing_fifo ? 'Open to FIFO or DIDO' : null].filter(Boolean);
  const body = h('div', {},
    h('section', { 'aria-labelledby': 'c-lic' }, h('h2', { id: 'c-lic' }, 'Licences and tickets'),
      c.licences.length ? h('div', { class: 'tickets' }, c.licences.map((l) => ticket({ name: l.type, expiry: l.expiry })))
        : h('p', { class: 'empty' }, 'No licences added.')),
    h('section', {}, h('h2', {}, 'Qualifications'),
      c.qualifications.length ? h('ul', { class: 'chips' }, c.qualifications.map((q) => h('li', { class: 'chip plain' }, q)))
        : h('p', { class: 'empty' }, 'None added.')),
    h('section', {}, h('h2', {}, 'Skills'),
      c.skills.length ? h('ul', { class: 'chips' }, c.skills.map((s) => h('li', { class: 'chip plain' }, s.years ? `${s.name}, ${years(s.years)}` : s.name)))
        : h('p', { class: 'empty' }, 'None added.')),
    h('section', {}, h('h2', {}, 'Experience'),
      unlocked && c.work_history
        ? h('ul', { class: 'rows' }, c.work_history.map((w) => h('li', { class: 'row' }, h('h3', {}, w.role),
          h('p', { class: 'meta' }, w.employer_name ? h('span', {}, w.employer_name) : null,
            h('span', {}, w.end_date ? `${fmtDate(w.start_date)} to ${fmtDate(w.end_date)}` : `Since ${fmtDate(w.start_date)}`)))))
        : c.experience.length
          ? h('ul', { class: 'rows' }, c.experience.map((e) => h('li', { class: 'row' }, h('div', { class: 'row-main' }, h('h3', {}, e.role), h('span', {}, years(e.years))))))
          : h('p', { class: 'empty' }, 'None added.')),
    unlocked ? h('section', {}, h('h2', {}, 'Documents'),
      c.documents.length ? h('ul', { class: 'rows' }, c.documents.map((d) => h('li', { class: 'row' },
        h('div', { class: 'row-main' }, h('h3', {}, d.filename),
          d.verified_at ? h('span', { class: 'badge ok' }, 'Checked by Mining Match') : h('span', { class: 'badge' }, 'Not checked yet')),
        h('div', { class: 'actions' }, actionButton('Download', () => download(`/documents/${d.id}/file`, d.filename), { class: 'btn small' })))))
        : h('p', { class: 'empty' }, 'No documents uploaded.')) : null);
  return h('div', {},
    h('a', { class: 'back', href: jobId ? `#/jobs/${jobId}` : '#/search' }, jobId ? 'Back to candidates' : 'Back to search'),
    pageHead(unlocked ? c.name : c.display_name),
    h('p', { class: 'meta' },
      c.location ? h('span', {}, stateName(c.location)) : null,
      c.available_from ? h('span', {}, `Available ${fmtDate(c.available_from)}`) : null,
      flags.map((f) => h('span', {}, f))),
    h('div', { class: 'split' }, body, contactPanel(ctx, c, terms, jobId)));
}

// -------------------------------------------------------------------- search
export async function search(ctx) {
  const lic = listEditor('Licences they must hold', [], { kind: 'licence' });
  const qual = listEditor('Qualifications they must hold', [], { kind: 'qualification' });
  const skills = listEditor('Skills', [], { kind: 'skill', withYears: true });
  const state = select(STATES, '');
  const minExp = input({ type: 'number', min: '0', max: '60', step: '0.5', inputmode: 'decimal' });
  const eligible = checkbox('Only show candidates who meet every requirement', true);
  const results = h('section', { 'aria-live': 'polite' });
  const error = errorArea();
  const form = onSubmit(h('form', { class: 'panel stack' },
    lic.el, qual.el, skills.el,
    h('div', { class: 'grid-2' }, field('State', state), field('Minimum years of experience', minExp)),
    eligible.el, error.el,
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary' }, 'Search'))), error, async () => {
    const criteria = {
      required_licences: lic.value(), required_qualifications: qual.value(), required_skills: skills.value(),
      location: state.value || null, min_experience_years: minExp.value === '' ? null : Number(minExp.value),
    };
    if (!criteria.required_licences.length && !criteria.required_qualifications.length && !criteria.required_skills.length
      && !criteria.location && criteria.min_experience_years === null) {
      throw new Error('Add at least one licence, qualification, skill, state or experience level.');
    }
    const found = await post('/search/candidates', { criteria, eligible_only: eligible.box.checked });
    results.replaceChildren(h('h2', { id: 'results' }, plural(found.length, 'candidate')),
      found.length ? h('ul', { class: 'rows' }, found.map((r) => candidateRow(r, null)))
        : h('p', { class: 'empty' }, eligible.box.checked ? 'Nobody meets all of that. Untick the filter to see the closest matches.' : 'No candidates found.'));
    results.querySelector('h2').tabIndex = -1;
    results.querySelector('h2').focus();
  });
  return h('div', {}, pageHead('Search candidates'),
    h('p', { class: 'lede' }, 'Find candidates without posting a job. Names stay hidden until a candidate approves you.'),
    form, results);
}

// ---------------------------------------------------------------- shortlists
export async function shortlists(ctx) {
  const [lists, jobs] = await Promise.all([get('/shortlists'), get('/jobs')]);
  const titles = Object.fromEntries(jobs.map((j) => [j.id, j.title]));
  const name = input({ required: true, maxlength: '120' });
  const error = errorArea();
  const form = onSubmit(h('form', { class: 'panel stack' }, h('h2', {}, 'New shortlist'), field('Name', name), error.el,
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary' }, 'Create shortlist'))), error, async () => {
    const created = await post('/shortlists', { name: name.value });
    ctx.go(`/shortlists/${created.id}`);
  });
  return h('div', {}, pageHead('Shortlists'),
    lists.length ? h('ul', { class: 'rows' }, lists.map((s) => h('li', { class: 'row' },
      h('div', { class: 'row-main' }, h('h3', {}, h('a', { href: `#/shortlists/${s.id}` }, s.name)), h('span', { class: 'meta' }, plural(s.entries, 'candidate'))),
      s.job_id && titles[s.job_id] ? h('p', { class: 'meta' }, `For: ${titles[s.job_id]}`) : null)))
      : h('p', { class: 'empty' }, 'No shortlists yet. Add candidates from a job’s candidate list, or create one here.'),
    h('section', {}, form));
}

export async function shortlist(ctx) {
  const id = ctx.params.id;
  const s = await get(`/shortlists/${id}`);
  const rows = s.entries.map((e) => {
    const status = select(SHORTLIST_STATUS, e.status, { 'aria-label': `Status for ${e.display_name}` });
    status.addEventListener('change', async () => {
      try { await put(`/shortlists/${id}/entries/${e.candidate_id}/status`, { status: status.value }); toast('Status saved.'); }
      catch (err) { toast(err.message, 'bad'); status.value = e.status; }
    });
    const notes = h('textarea', { 'aria-label': `Notes about ${e.display_name}`, maxlength: '2000', rows: '2' }, e.notes || '');
    return h('li', { class: 'row' },
      h('div', { class: 'row-main' },
        h('h3', {}, h('a', { href: `#/candidates/${e.candidate_id}${s.job_id ? `?job=${s.job_id}` : ''}` }, e.display_name)),
        e.score_at_add !== null ? h('span', { class: 'meta' }, `Match ${Math.round(e.score_at_add)} when added`) : null),
      h('p', { class: 'meta' }, h('span', {}, CONTACT_WORDS[e.contact_status] || '')),
      h('div', { class: 'grid-2' }, field('Status', status), field('Notes', notes)),
      h('div', { class: 'actions' },
        actionButton('Save notes', async () => {
          await post(`/shortlists/${id}/entries`, { candidate_id: e.candidate_id, notes: notes.value });
          toast('Notes saved.');
        }, { class: 'btn small' }),
        confirmButton('Remove', 'Tap again to remove', async () => {
          await del(`/shortlists/${id}/entries/${e.candidate_id}`);
          ctx.reload('h1');
        }, { class: 'btn danger small' })));
  });
  return h('div', {},
    h('a', { class: 'back', href: '#/shortlists' }, 'All shortlists'),
    pageHead(s.name, s.job_id ? h('a', { class: 'btn small', href: `#/jobs/${s.job_id}` }, 'See all candidates for this job') : null),
    rows.length ? h('ul', { class: 'rows' }, rows) : h('p', { class: 'empty' }, 'No one on this shortlist yet.'));
}

// ------------------------------------------------------------------- billing
export async function billing(ctx) {
  let terms = null;
  try { terms = await get('/fee-terms/current'); } catch (err) { if (err.status !== 404) throw err; }
  const [settings, invoices, hires, intros, details] = await Promise.all([
    get('/billing/settings'), get('/invoices'), get('/hires'), get('/introductions'), get('/billing/details')]);
  const activeHire = Object.fromEntries(hires.filter((x) => x.status === 'recorded').map((x) => [x.candidate_id, x]));

  const invoiceSection = h('section', { 'aria-labelledby': 'inv-title' }, h('h2', { id: 'inv-title' }, 'Invoices'),
    invoices.length ? h('ul', { class: 'rows' }, invoices.map((i) => h('li', { class: 'row' },
      h('div', { class: 'row-main' },
        h('h3', {}, h('a', { href: `#/invoices/${i.id}` }, i.number || 'Scheduled invoice')),
        h('strong', {}, money(i.total_cents))),
      h('p', { class: 'meta' }, invoiceBadge(i), i.number ? h('span', {}, `Issued ${fmtDate(i.issue_date)}`) : null))))
      : h('p', { class: 'empty' }, 'No invoices yet.'));

  const hireSection = h('section', { 'aria-labelledby': 'hires-title' }, h('h2', { id: 'hires-title' }, 'Candidates you’ve unlocked'),
    h('p', { class: 'muted' }, 'Hired one of them? Record the hire so the placement fee is invoiced on their start date.'),
    intros.length ? h('ul', { class: 'rows' }, intros.map((i) => {
      const hired = activeHire[i.candidate_id];
      return h('li', { class: 'row' },
        h('div', { class: 'row-main' }, h('h3', {}, h('a', { href: `#/candidates/${i.candidate_id}` }, i.display_name)),
          h('span', { class: 'meta' }, `Unlocked ${fmtDate(i.unlocked_at)}`)),
        hired
          ? h('p', { class: 'meta' }, h('span', {}, `Hire recorded: starts ${fmtDate(hired.start_date)}`),
            hired.invoice_id ? h('a', { href: `#/invoices/${hired.invoice_id}` }, hired.invoice_number || 'Scheduled invoice')
              : h('span', {}, 'No fee: outside the fee window'))
          : h('div', { class: 'actions' }, h('a', { class: 'btn small', href: `#/hire/${i.candidate_id}${i.job_id ? `?job=${i.job_id}` : ''}` }, 'Record a hire')));
    })) : h('p', { class: 'empty' }, 'None yet.'));

  const termsPanel = terms
    ? h('section', { class: 'panel stack', 'aria-labelledby': 'terms-title' },
      h('h2', { id: 'terms-title' }, `Placement terms, version ${terms.version}`),
      termsSummary(terms),
      h('details', {}, h('summary', {}, 'Read the full terms'), h('div', { class: 'terms-text' }, terms.terms_text)),
      terms.accepted
        ? h('p', { class: 'badge ok' }, 'You’ve accepted these terms')
        : h('div', { class: 'actions' }, actionButton('Accept these terms', async () => {
          await post(`/fee-terms/${terms.version}/accept`);
          toast('Terms accepted.');
          ctx.reload('#terms-title');
        }, { class: 'btn primary' })))
    : h('p', { class: 'notice' }, 'Mining Match hasn’t published placement terms yet.');

  const abn = input({ value: details.abn || '', inputmode: 'numeric', autocomplete: 'off' });
  const email = input({ type: 'email', value: details.billing_email || '', autocomplete: 'email' });
  const address = h('textarea', { rows: '2', maxlength: '300' }, details.billing_address || '');
  const error = errorArea();
  const detailsForm = onSubmit(h('form', { class: 'panel stack', 'aria-labelledby': 'bill-title' },
    h('h2', { id: 'bill-title' }, 'Billing details'),
    h('p', { class: 'small muted' }, `Invoices are made out to ${details.company_name}.`),
    h('div', { class: 'grid-2' },
      field('ABN (optional)', abn, '11 digits. Shown on your invoices.'),
      field('Send invoices to (optional)', email, `Leave empty to use ${details.account_email}.`)),
    field('Postal address (optional)', address), error.el,
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary' }, 'Save billing details'))), error, async () => {
    await put('/billing/details', { abn: abn.value || null, billing_email: email.value || null, billing_address: address.value || null });
    toast('Billing details saved.');
    ctx.reload('#bill-title');
  });

  return h('div', {}, pageHead('Billing'),
    h('p', { class: 'lede' }, 'You pay nothing to search or post jobs. A placement fee applies only when you hire someone whose details you unlocked here. Invoices are paid by bank transfer.'),
    invoiceSection, hireSection,
    h('section', {}, termsPanel),
    settings.enabled ? h('section', {}, payBox(settings.payment, null)) : null,
    h('section', {}, detailsForm));
}

export async function hire(ctx) {
  const id = ctx.params.id;
  const [c, jobs, settings] = await Promise.all([get(`/candidates/${id}`), get('/jobs'), get('/billing/settings')]);
  const back = h('a', { class: 'back', href: '#/billing' }, 'Back to billing');
  if (c.contact_status !== 'unlocked') {
    return h('div', {}, back, pageHead('Record a hire'), h('p', { class: 'notice' }, 'Unlock this candidate’s details before recording a hire.'));
  }
  if (!settings.enabled) {
    return h('div', {}, back, pageHead('Record a hire'), h('p', { class: 'notice warn' }, 'Invoicing isn’t set up yet. Contact Mining Match.'));
  }
  const job = select([['', 'Not for one of my listed jobs'], ...jobs.map((j) => [j.id, j.title])], ctx.query.job || '');
  const start = input({ type: 'date', required: true });
  const salary = input({ inputmode: 'decimal', required: true, placeholder: 'e.g. 120000', autocomplete: 'off' });
  const preview = h('div', { 'aria-live': 'polite' });
  const error = errorArea();
  let quote = null;
  const body = () => ({ candidate_id: id, start_date: start.value, base_salary: salary.value.replace(/[$,\s]/g, ''), job_id: job.value || null });
  const reset = () => { quote = null; preview.replaceChildren(); };
  [job, start, salary].forEach((el) => el.addEventListener('input', reset));

  const confirm = async () => {
    const result = await post('/hires', body());
    toast(result.invoice ? 'Hire recorded. The invoice is in Billing.' : 'Hire recorded. No placement fee applies.');
    ctx.go(result.invoice ? `/invoices/${result.invoice.id}` : '/billing');
  };
  const form = onSubmit(h('form', { class: 'panel stack' },
    field('Job (optional)', job),
    h('div', { class: 'grid-2' },
      field('Start date', start, 'The invoice is dated on this day.'),
      field('First-year base salary (AUD)', salary, 'Before super and allowances, as defined in the placement terms.')),
    error.el,
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn' }, 'Work out the fee'))), error, async () => {
    quote = await post('/hires/preview', body());
    const q = quote;
    const parts = [];
    if (!q.in_window) {
      parts.push(h('p', { class: 'lede' }, `No placement fee applies: they start after the fee window closed on ${fmtDate(q.window_ends)}.`));
    } else {
      parts.push(
        h('p', { class: 'total' }, money(q.total_cents)),
        h('ul', { class: 'reasons' },
          h('li', {}, q.minimum_applied
            ? `Minimum placement fee: ${money(q.fee_cents)}`
            : `${q.fee_percent}% of ${money(q.base_salary_cents)}: ${money(q.fee_cents)}`),
          q.gst_registered ? h('li', {}, `GST: ${money(q.gst_cents)}`) : null,
          h('li', {}, q.issue_date === q.start_date ? `Invoice dated ${fmtDate(q.issue_date)}, their start date` : `Invoice dated ${fmtDate(q.issue_date)}`),
          h('li', {}, `Due ${fmtDate(q.due_date)}, by bank transfer`),
          h('li', {}, `${q.guarantee_days}-day replacement guarantee if paid by the due date`)));
    }
    parts.push(h('div', { class: 'actions' }, actionButton('Confirm hire', confirm, { class: 'btn primary' })));
    preview.replaceChildren(h('section', { class: 'panel fee-preview', 'aria-labelledby': 'fee-title' },
      h('h2', { id: 'fee-title' }, q.in_window ? 'Placement fee' : 'No fee'), parts));
  });
  return h('div', {}, back, pageHead(`Record a hire: ${c.name}`),
    h('p', { class: 'lede' }, 'Tell us when they start and their first-year base salary. Check the fee, then confirm.'),
    form, preview);
}
