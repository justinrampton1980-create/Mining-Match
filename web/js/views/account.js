// Account: password, log out, download my data, two-factor (admins), delete (candidates), close (employers).
import { h, field, input, errorArea, onSubmit, actionButton, confirmButton, toast } from '../dom.js';
import { get, post, del, download } from '../api.js';
import { recoveryCodesView } from './auth.js';

const ROLE = { candidate: 'Candidate account', employer: 'Employer account', admin: 'Administrator' };

export async function account(ctx) {
  const me = ctx.me;
  const current = input({ type: 'password', autocomplete: 'current-password', required: true });
  const next = input({ type: 'password', autocomplete: 'new-password', required: true, minlength: '10' });
  const error = errorArea();
  const passwordForm = onSubmit(h('form', { class: 'panel stack', 'aria-labelledby': 'pw-title' },
    h('h2', { id: 'pw-title' }, 'Change password'),
    field('Current password', current),
    field('New password', next, 'At least 10 characters. Other devices will be logged out.'),
    error.el, h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary' }, 'Change password'))),
  error, async () => {
    await post('/auth/change-password', { current_password: current.value, new_password: next.value });
    current.value = ''; next.value = '';
    toast('Password changed. Other devices were logged out.');
  });

  const logout = actionButton('Log out', async () => {
    await post('/auth/logout');
    await ctx.refresh();
    ctx.go('/login');
  });

  const parts = [
    h('h1', {}, 'Account'),
    h('p', { class: 'lede' }, ROLE[me.role]),
    h('p', {}, me.email),
    h('div', { class: 'actions' }, logout),
    h('section', {}, passwordForm),
    h('section', { class: 'panel stack', 'aria-labelledby': 'export-title' },
      h('h2', { id: 'export-title' }, 'Download your data'),
      h('p', {}, me.role === 'candidate'
        ? 'A ZIP file of everything Mining Match holds about you: your profile, licences, work history, uploaded documents, which employers asked for or unlocked your details, and your activity.'
        : 'A ZIP file of everything Mining Match holds about your account and your activity.'),
      h('div', { class: 'actions' }, actionButton('Download my data', async () => {
        await download('/me/export', 'mining-match-my-data.zip');
        toast('Your data is downloading.');
      }))),
  ];
  if (me.role === 'admin') parts.push(await twoFactorPanel(ctx));
  if (me.role === 'candidate') {
    parts.push(h('section', { class: 'panel stack', 'aria-labelledby': 'del-title' },
      h('h2', { id: 'del-title' }, 'Delete your account'),
      h('p', {}, 'This removes your profile, licences, work history and uploaded documents. It can’t be undone.'),
      h('div', { class: 'actions' }, confirmButton('Delete my account', 'Tap again to delete permanently', async () => {
        await del(`/candidates/${me.candidate_id}`);
        await ctx.refresh();
        toast('Your account was deleted.');
        ctx.go('/login');
      }))));
  }
  if (me.role === 'employer') parts.push(closeEmployerPanel(ctx));
  return h('div', { class: 'stack' }, parts);
}

// Employers close their account with their password. Invoices and hires stay on record.
function closeEmployerPanel(ctx) {
  const password = input({ type: 'password', autocomplete: 'current-password', required: true });
  const error = errorArea();
  let armed = false;
  const submit = h('button', { type: 'submit', class: 'btn danger' }, 'Close my account');
  const form = onSubmit(h('form', { class: 'panel stack', 'aria-labelledby': 'close-title' },
    h('h2', { id: 'close-title' }, 'Close your account'),
    h('p', {}, 'Your jobs are closed and your shortlists and contact requests are removed. Your sign-in, contact name, phone and billing email are deleted. Invoices, hires and the placement terms you accepted are kept, as business records. You can’t close the account while an invoice is unpaid.'),
    field('Your password', password), error.el, h('div', { class: 'actions' }, submit)),
  error, async () => {
    if (!armed) {  // a second tap confirms, as for other actions that can't be undone
      armed = true;
      submit.textContent = 'Tap again to close permanently';
      return;
    }
    await post(`/employers/${ctx.me.employer_id}/close`, { password: password.value });
    await ctx.refresh();
    toast('Your account was closed.');
    ctx.go('/login');
  });
  return form;
}

// Admins: two-factor is always on; this shows the recovery codes left and makes new ones.
async function twoFactorPanel(ctx) {
  const status = await get('/auth/two-factor');
  const code = input({ inputmode: 'numeric', autocomplete: 'one-time-code', required: true, maxlength: '6', class: 'code-input' });
  const error = errorArea();
  const panel = h('section', { class: 'panel stack', 'aria-labelledby': 'tf-title' });
  const form = onSubmit(h('form', { class: 'stack' },
    field('Code from your authenticator app', code, 'Making new recovery codes cancels the old ones.'),
    error.el, h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn' }, 'Make new recovery codes'))),
  error, async () => {
    const made = await post('/auth/two-factor/recovery-codes', { code: code.value });
    panel.replaceChildren(...recoveryCodesView(made.recovery_codes, 'Your new recovery codes',
      () => ctx.reload('#tf-title'), 'tf-title'));
    panel.querySelector('h2').focus();
  });
  const left = status.recovery_codes_left;
  panel.append(
    h('h2', { id: 'tf-title', tabindex: '-1' }, 'Two-factor login'),
    h('p', {}, 'On: you log in with your password and a code from your authenticator app.'),
    h('p', { class: left <= 3 ? 'form-error' : 'muted' }, `${left} recovery ${left === 1 ? 'code' : 'codes'} left.`),
    form,
    h('p', { class: 'small muted' }, 'New phone? Ask another admin to reset your two-factor login, then set it up again when you next log in.'));
  return panel;
}
