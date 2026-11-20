// Account: password, log out, delete (candidates).
import { h, field, input, errorArea, onSubmit, actionButton, confirmButton, toast } from '../dom.js';
import { post, del } from '../api.js';

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
