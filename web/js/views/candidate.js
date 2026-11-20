// Candidate screens: profile (tickets, qualifications, skills, work history, documents,
// details), jobs that match, and employers' requests for contact details.
import { h, field, input, select, checkbox, errorArea, onSubmit, actionButton, confirmButton, reveal, toast } from '../dom.js';
import { api, get, post, put, patch, del, download, query } from '../api.js';
import { ticket, score, reasons, catalog, licenceRules, pageHead } from '../components.js';
import { fmtDate, years, STATES, stateName, plural, expiryStatus, addYears, isoDate, today } from '../format.js';

const DOC_KINDS = [['licence', 'Licence or ticket'], ['qualification', 'Qualification'], ['resume', 'Resume'], ['other', 'Other']];
const MAX_MB = 10;

function suggestions(kind, id) {
  const list = h('datalist', { id });
  catalog(kind).then((names) => list.replaceChildren(...names.map((n) => h('option', { value: n }))));
  return list;
}

function addForm(title, fields, save, close) {
  const error = errorArea();
  return onSubmit(h('form', { class: 'panel stack' }, h('h3', {}, title), fields, error.el,
    h('div', { class: 'actions' },
      h('button', { type: 'submit', class: 'btn primary' }, 'Save'),
      h('button', { type: 'button', class: 'btn quiet', onclick: close }, 'Cancel'))), error, save);
}

function removeButton(label, run) {
  return confirmButton('Remove', 'Tap again to remove', run, { class: 'btn danger small', 'aria-label': `Remove ${label}` });
}

// ------------------------------------------------------------------ profile
export async function profile(ctx) {
  const id = ctx.me.candidate_id;
  const p = await get(`/candidates/${id}`);
  const base = `/candidates/${id}`;
  const after = (focus) => () => ctx.reload(focus);
  const withExpiry = p.licences.filter((l) => l.expiry);

  // Licences and tickets
  const licenceSection = h('section', { 'aria-labelledby': 'licences' },
    h('h2', { id: 'licences' }, 'Licences and tickets'),
    p.licences.length
      ? h('div', { class: 'tickets' }, p.licences.map((l) => ticket({ name: l.type, expiry: l.expiry },
        removeButton(l.type, async () => { await del(`${base}/licences${query({ name: l.type })}`); after('#licences')(); }))))
      : h('p', { class: 'empty' }, 'Add your licences and tickets. Jobs check these first, including expiry dates.'),
    withExpiry.length ? h('p', { class: 'small muted' }, p.expiry_reminders
      ? 'We email you 90, 60 and 30 days before each expiry date.'
      : 'Expiry reminders are off. You can turn them on under Your details.') : null,
    h('div', { class: 'actions' }, reveal('Add a licence or ticket', (close) => {
      const name = input({ list: 'licence-options', required: true, autocomplete: 'off' });
      const issued = input({ type: 'date', max: isoDate(today()) });
      const expiry = input({ type: 'date' });
      const docs = p.documents.filter((d) => d.kind === 'licence' || d.kind === 'other');
      const proof = select([['', 'None'], ...docs.map((d) => [d.id, d.filename])], '');

      // Known licences: show how renewal works and, where there's a fixed cycle, work out the
      // expiry date from the date someone got it. A date they type themselves always wins.
      const note = h('p', { class: 'rule-note', 'aria-live': 'polite' });
      const issuedField = field('Date you got it or last renewed it', issued, 'Only used to work out the expiry date.');
      issuedField.hidden = true;
      const expiryField = field('Expiry date', expiry, 'Leave empty if it doesn’t expire.');
      const expiryHint = expiryField.querySelector('.hint');
      const plainHint = expiryHint.textContent;
      let rule = null;
      let worked = false; // the expiry shown was worked out, not typed
      const fill = () => {
        const cycle = rule && rule.renew_years;
        if (cycle && issued.value) {
          if (expiry.value && !worked) return;
          expiry.value = addYears(issued.value, cycle);
          worked = true;
          expiryHint.textContent = `Worked out as ${cycle} years from ${fmtDate(issued.value)}. Check it against your card.`;
        } else if (worked) {
          expiry.value = '';
          worked = false;
          expiryHint.textContent = plainHint;
        }
      };
      name.addEventListener('input', async () => {
        const rules = await licenceRules();
        rule = rules.get(name.value.trim().toLowerCase()) || null;
        note.textContent = rule ? rule.note || '' : '';
        issuedField.hidden = !(rule && rule.renew_years);
        fill();
      });
      issued.addEventListener('input', fill);
      expiry.addEventListener('input', () => { worked = false; expiryHint.textContent = plainHint; });

      return addForm('Add a licence or ticket', [
        field('Licence or ticket', name, 'Pick from the list where you can, so employers’ searches find it.'),
        suggestions('licence', 'licence-options'), note, issuedField, expiryField,
        docs.length ? field('Proof (optional)', proof, 'A copy you’ve uploaded under Documents.') : null,
      ], async () => {
        await put(`${base}/licences`, { name: name.value, expiry: expiry.value || null, document_id: proof.value || null });
        toast(`${name.value} saved.`);
        after('#licences')();
      }, close);
    })));

  // Qualifications
  const qualSection = h('section', { 'aria-labelledby': 'quals' },
    h('h2', { id: 'quals' }, 'Qualifications'),
    p.qualifications.length
      ? h('ul', { class: 'rows' }, p.qualifications.map((q) => h('li', { class: 'row' },
        h('div', { class: 'row-main' }, h('h3', {}, q),
          removeButton(q, async () => { await del(`${base}/qualifications${query({ name: q })}`); after('#quals')(); })))))
      : h('p', { class: 'empty' }, 'Add certificates and trade qualifications, such as a Cert III.'),
    h('div', { class: 'actions' }, reveal('Add a qualification', (close) => {
      const name = input({ list: 'qual-options', required: true, autocomplete: 'off' });
      const awarded = input({ type: 'date' });
      const institution = input({});
      return addForm('Add a qualification', [
        field('Qualification', name), suggestions('qualification', 'qual-options'),
        h('div', { class: 'grid-2' }, field('Completed (optional)', awarded), field('Where (optional)', institution)),
      ], async () => {
        await put(`${base}/qualifications`, { name: name.value, awarded_date: awarded.value || null, institution: institution.value || null });
        after('#quals')();
      }, close);
    })));

  // Skills
  const skillSection = h('section', { 'aria-labelledby': 'skills' },
    h('h2', { id: 'skills' }, 'Skills'),
    p.skills.length
      ? h('ul', { class: 'chips' }, p.skills.map((s) => h('li', { class: 'chip' },
        s.years ? `${s.name}, ${years(s.years)}` : s.name,
        h('button', { type: 'button', 'aria-label': `Remove ${s.name}`, onclick: async () => {
          await del(`${base}/skills${query({ name: s.name })}`); after('#skills')();
        } }, '×'))))
      : h('p', { class: 'empty' }, 'Add the machines you operate and the work you do, with years of experience.'),
    h('div', { class: 'actions' }, reveal('Add a skill', (close) => {
      const name = input({ list: 'skill-options', required: true, autocomplete: 'off' });
      const yrs = input({ type: 'number', min: '0', max: '60', step: '0.5', inputmode: 'decimal' });
      return addForm('Add a skill', [
        field('Skill', name), suggestions('skill', 'skill-options'),
        field('Years of experience', yrs, 'Jobs can ask for a minimum number of years.'),
      ], async () => {
        await put(`${base}/skills`, { name: name.value, years: yrs.value === '' ? null : Number(yrs.value) });
        after('#skills')();
      }, close);
    })));

  // Work history
  const historySection = h('section', { 'aria-labelledby': 'history' },
    h('h2', { id: 'history' }, 'Work history'),
    p.work_history.length
      ? h('ul', { class: 'rows' }, p.work_history.map((w) => h('li', { class: 'row' },
        h('div', { class: 'row-main' }, h('h3', {}, w.role),
          removeButton(w.role, async () => { await del(`/experience/${w.id}`); after('#history')(); })),
        h('p', { class: 'meta' },
          w.employer_name ? h('span', {}, w.employer_name) : null,
          h('span', {}, w.end_date ? `${fmtDate(w.start_date)} to ${fmtDate(w.end_date)}` : `Since ${fmtDate(w.start_date)}`)))))
      : h('p', { class: 'empty' }, 'Add the roles you’ve held. Jobs count years in relevant roles.'),
    h('div', { class: 'actions' }, reveal('Add a role', (close) => {
      const role = input({ required: true, placeholder: 'e.g. Dragline Operator' });
      const employer = input({});
      const start = input({ type: 'date', required: true });
      const end = input({ type: 'date' });
      const current = checkbox('I still work here', false, { onchange: () => { end.disabled = current.box.checked; if (current.box.checked) end.value = ''; } });
      return addForm('Add a role', [
        field('Role', role), field('Employer (optional)', employer, 'Only shown to employers you approve.'),
        h('div', { class: 'grid-2' }, field('Started', start), field('Finished', end)), current.el,
      ], async () => {
        await post(`${base}/experience`, { role: role.value, employer_name: employer.value || null, start_date: start.value, end_date: current.box.checked ? null : end.value || null });
        after('#history')();
      }, close);
    })));

  // Documents
  const fileInput = input({ type: 'file', accept: '.pdf,.doc,.docx,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png', required: true });
  const kind = select(DOC_KINDS, 'licence');
  const uploadError = errorArea();
  const uploadForm = onSubmit(h('form', { class: 'panel stack' }, h('h3', {}, 'Upload a document'),
    h('div', { class: 'grid-2' }, field('What is it?', kind), field('File', fileInput, `PDF, Word, JPEG or PNG, up to ${MAX_MB} MB.`)),
    uploadError.el, h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary' }, 'Upload'))),
  uploadError, async () => {
    const file = fileInput.files[0];
    if (!file) throw new Error('Choose a file first.');
    if (file.size > MAX_MB * 1024 * 1024) throw new Error(`That file is larger than ${MAX_MB} MB.`);
    const form = new FormData();
    form.append('kind', kind.value);
    form.append('file', file);
    await api('POST', `${base}/documents`, undefined, { form });
    toast(`${file.name} uploaded.`);
    after('#documents')();
  });
  const docSection = h('section', { 'aria-labelledby': 'documents' },
    h('h2', { id: 'documents' }, 'Documents'),
    h('p', { class: 'muted' }, 'Copies of your licences and certificates. Employers can see them only after you approve them. Mining Match checks them and marks them as checked.'),
    p.documents.length
      ? h('ul', { class: 'rows' }, p.documents.map((d) => h('li', { class: 'row' },
        h('div', { class: 'row-main' }, h('h3', {}, d.filename),
          d.verified_at ? h('span', { class: 'badge ok' }, 'Checked') : h('span', { class: 'badge' }, 'Not checked yet')),
        h('p', { class: 'meta' }, h('span', {}, DOC_KINDS.find(([k]) => k === d.kind)?.[1] || d.kind),
          h('span', {}, `${Math.max(1, Math.round(d.size_bytes / 1024))} KB`), h('span', {}, `Uploaded ${fmtDate(d.uploaded_at)}`)),
        h('div', { class: 'actions' },
          actionButton('Download', () => download(`/documents/${d.id}/file`, d.filename), { class: 'btn small' }),
          removeButton(d.filename, async () => { await del(`/documents/${d.id}`); after('#documents')(); })))))
      : null,
    uploadForm);

  // Details
  const name = input({ value: p.name, required: true, autocomplete: 'name' });
  const phone = input({ type: 'tel', value: p.phone || '', autocomplete: 'tel' });
  const state = select(STATES, p.location || '');
  const available = input({ type: 'date', value: p.available_from || '' });
  const summary = h('textarea', { maxlength: '2000' }, p.summary || '');
  const relocate = checkbox('Willing to relocate', p.willing_to_relocate);
  const fifo = checkbox('Open to FIFO or DIDO rosters', p.willing_fifo);
  const visible = checkbox('Show my profile to employers', p.is_active);
  const remind = checkbox('Email me before my licences expire', p.expiry_reminders, {},
    '90, 60 and 30 days before each expiry date, and once if one lapses.');
  const detailsError = errorArea();
  const details = onSubmit(h('form', { class: 'panel stack', 'aria-labelledby': 'details' },
    h('h2', { id: 'details' }, 'Your details'),
    h('div', { class: 'grid-2' }, field('Full name', name), field('Mobile number', phone, 'Only shared with employers you approve.')),
    h('div', { class: 'grid-2' }, field('State you live in', state), field('Available from', available)),
    relocate.el, fifo.el,
    field('About you (optional)', summary, 'Only shared with employers you approve.'),
    visible.el, remind.el, detailsError.el,
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary' }, 'Save details'))),
  detailsError, async () => {
    await patch(base, {
      name: name.value, phone: phone.value || null, location: state.value || null,
      available_from: available.value || null, summary: summary.value || null,
      willing_to_relocate: relocate.box.checked, willing_fifo: fifo.box.checked, is_active: visible.box.checked,
      expiry_reminders: remind.box.checked,
    });
    toast('Details saved.');
    after('#details')();
  });

  const expiring = p.licences.filter((l) => ['expired', 'expiring'].includes(expiryStatus(l.expiry).state));

  return h('div', {},
    pageHead(p.name),
    h('p', { class: 'meta' },
      p.location ? h('span', {}, stateName(p.location)) : h('span', {}, 'State not set'),
      p.is_active ? h('span', { class: 'badge ok' }, 'Employers can find you') : h('span', { class: 'badge warn' }, 'Hidden from employers')),
    expiring.length ? h('p', { class: 'notice warn' },
      `${plural(expiring.length, 'licence')} expired or expiring within 90 days. Employers can’t count an expired licence.`) : null,
    licenceSection, qualSection, skillSection, historySection, docSection, h('section', {}, details));
}

// ------------------------------------------------------------------- matches
export async function matches(ctx) {
  const id = ctx.me.candidate_id;
  const [results, open] = await Promise.all([get(`/candidates/${id}/matching-jobs`), get('/jobs/open')]);
  const jobs = Object.fromEntries(open.map((j) => [j.id, j]));
  return h('div', {},
    pageHead('Jobs for you'),
    h('p', { class: 'lede' }, 'Open jobs ranked by how well your licences, skills and experience fit. Employers contact you through Requests.'),
    results.length
      ? h('ul', { class: 'rows' }, results.map((r) => {
        const job = jobs[r.job_id] || {};
        return h('li', { class: 'row' },
          h('div', { class: 'row-main' }, h('h3', {}, h('a', { href: `#/matches/${r.job_id}` }, r.job_title)), score(r)),
          h('p', { class: 'meta' }, h('span', {}, r.company_name),
            job.location ? h('span', {}, stateName(job.location)) : null,
            job.start_date ? h('span', {}, `Starts ${fmtDate(job.start_date)}`) : null),
          reasons(r, { forCandidate: true }));
      }))
      : h('p', { class: 'empty' }, 'No open jobs right now. Keep your licences and skills up to date so you rank well when jobs are posted.'));
}

export async function job(ctx) {
  const id = ctx.me.candidate_id;
  const [j, results] = await Promise.all([get(`/jobs/${ctx.params.id}`), get(`/candidates/${id}/matching-jobs`)]);
  const r = results.find((x) => x.job_id === j.id);
  const list = (title, items) => (items && items.length
    ? h('div', {}, h('h3', {}, title), h('ul', { class: 'chips' },
      items.map((i) => h('li', { class: 'chip plain' }, typeof i === 'string' ? i : (i.min_years ? `${i.name}, ${i.min_years}+ yrs` : i.name)))))
    : null);
  return h('div', {},
    h('a', { class: 'back', href: '#/matches' }, 'Back to jobs'),
    pageHead(j.title, r ? score(r) : null),
    h('p', { class: 'meta' }, h('span', {}, j.company_name), j.location ? h('span', {}, stateName(j.location)) : null,
      j.start_date ? h('span', {}, `Starts ${fmtDate(j.start_date)}`) : null,
      j.min_experience_years ? h('span', {}, `${years(j.min_experience_years)} experience`) : null),
    r ? h('div', { class: 'stack' }, reasons(r, { forCandidate: true })) : null,
    j.description ? h('section', {}, h('h2', {}, 'About the job'), h('p', { class: 'pre' }, j.description)) : null,
    h('section', { class: 'stack' }, h('h2', {}, 'What it needs'),
      list('Licences', j.required_licences), list('Licences that help', j.preferred_licences),
      list('Qualifications', j.required_qualifications), list('Qualifications that help', j.preferred_qualifications),
      list('Skills', j.required_skills), list('Skills that help', j.preferred_skills)),
    h('p', { class: 'muted' }, 'You don’t need to apply. If this employer wants to talk, they’ll send a request and you decide whether to share your details.'));
}

// ------------------------------------------------------------------ requests
const REQUEST_STATUS = {
  requested: ['Waiting for you', 'warn'],
  granted: ['You approved', 'ok'],
  declined: ['You declined', ''],
};

export async function requests(ctx) {
  const items = await get('/contact-requests');
  const decide = (employerId, grant, message) => async () => {
    await put(`/contact-releases/${employerId}`, { grant });
    toast(message);
    ctx.reload('#requests-title');
  };
  return h('div', {},
    h('h1', { id: 'requests-title' }, 'Requests'),
    h('p', { class: 'lede' }, 'Employers see your licences and experience but not your name, number or documents. They see those only if you approve them.'),
    items.length
      ? h('ul', { class: 'rows' }, items.map((r) => {
        const [label, tone] = REQUEST_STATUS[r.status] || [r.status, ''];
        return h('li', { class: 'row' },
          h('div', { class: 'row-main' }, h('h3', {}, r.company_name), h('span', { class: `badge ${tone}` }, label)),
          h('p', { class: 'meta' }, r.job_title ? h('span', {}, `About: ${r.job_title}`) : h('span', {}, 'No job named'),
            r.requested_at ? h('span', {}, `Asked ${fmtDate(r.requested_at)}`) : null),
          h('div', { class: 'actions' },
            r.status !== 'granted' ? actionButton('Approve', decide(r.employer_id, true, `Approved. ${r.company_name} can unlock your details after agreeing to the placement terms.`), { class: 'btn primary small' }) : null,
            r.status === 'requested' ? actionButton('Decline', decide(r.employer_id, false, 'Declined.'), { class: 'btn small' }) : null,
            r.status === 'granted' ? actionButton('Withdraw approval', decide(r.employer_id, false, 'Approval withdrawn. Your details are hidden again.'), { class: 'btn small' }) : null));
      }))
      : h('p', { class: 'empty' }, 'No requests yet. When an employer wants your contact details, it will show up here.'));
}
