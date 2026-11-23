// Account: password, log out, two-factor (admins), delete (candidates).
import { h, field, input, errorArea, onSubmit, actionButton, confirmButton, toast } from '../dom.js';
import { get, post, del } from '../api.js';
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
  return h('div', { class: 'stack' }, parts);
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
      () => ctx.reload('#tf-title')));
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
