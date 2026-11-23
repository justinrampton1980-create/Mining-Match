// Account: password, two-factor login, log out, delete (candidates).
import { h, field, input, errorArea, onSubmit, actionButton, confirmButton, toast } from '../dom.js';
import { get, post, del } from '../api.js';

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
    h('section', {}, await twoFactorPanel(ctx)),
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

// ------------------------------------------------------------ two-factor login
const passwordBox = () => input({ type: 'password', autocomplete: 'current-password', required: true });
const codeBox = () => input({ autocomplete: 'one-time-code', inputmode: 'numeric', required: true, maxlength: '20', spellcheck: 'false' });
const CODE_HINT = 'The 6-digit code your authenticator app shows now, or a backup code.';

async function twoFactorPanel(ctx) {
  const status = await get('/auth/two-factor');
  const panel = h('div', { class: 'panel stack', id: 'two-factor', 'aria-labelledby': 'tf-title' });
  const title = h('h2', { id: 'tf-title' }, 'Two-factor login');
  if (!status.enabled) {
    panel.append(title,
      status.required ? h('p', { class: 'notice warn', role: 'status' },
        'Administrators must turn this on before they can use the admin pages.') : null,
      h('p', {}, 'Stops anyone who learns your password from getting in. Logging in will also need a code '
        + 'from an app on your phone, such as Google Authenticator, Microsoft Authenticator or 1Password.'),
      startSetup(ctx, panel, title));
    return panel;
  }
  const left = status.backup_codes_left;
  panel.append(title,
    h('p', {}, h('span', { class: 'badge ok' }, 'On'), ' Logging in needs your password and a code from your app.'),
    h('p', { class: left <= 3 ? 'notice warn' : 'small' },
      `${left} backup code${left === 1 ? '' : 's'} left.${left <= 3 ? ' Make new ones so you don’t get locked out.' : ''}`),
    passwordAndCode('New backup codes', 'Your old backup codes will stop working.', async (password, code) => {
      const result = await post('/auth/two-factor/recovery-codes', { password, code });
      panel.replaceChildren(title, ...showBackupCodes(ctx, result.recovery_codes));
    }));
  if (!status.required) {
    panel.append(passwordAndCode('Turn off two-factor login', 'Your account will be protected by your password only.', async (password, code) => {
      await post('/auth/two-factor/disable', { password, code });
      toast('Two-factor login is off.');
      await ctx.refresh();
      ctx.reload('#tf-title');
    }, 'btn danger'));
  }
  return panel;
}

function startSetup(ctx, panel, title) {
  const password = passwordBox();
  const error = errorArea();
  return onSubmit(h('form', { class: 'stack' },
    field('Your password', password), error.el,
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary' }, 'Set up two-factor login'))),
  error, async () => {
    const setup = await post('/auth/two-factor/setup', { password: password.value });
    panel.replaceChildren(title, ...confirmSetup(ctx, panel, title, setup));
    panel.querySelector('a.btn').focus();
  });
}

function confirmSetup(ctx, panel, title, { secret, setup_link: link }) {
  const code = codeBox();
  const error = errorArea();
  const grouped = secret.match(/.{1,4}/g).join(' ');
  const form = onSubmit(h('form', { class: 'stack' },
    field('Code from the app', code, 'Type the 6-digit code the app shows for Mining Match.'), error.el,
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'btn primary' }, 'Turn on'))),
  error, async () => {
    const result = await post('/auth/two-factor/enable', { code: code.value });
    panel.replaceChildren(title, ...showBackupCodes(ctx, result.recovery_codes));
  });
  return [
    h('ol', { class: 'steps stack' },
      h('li', {}, 'Install an authenticator app on your phone if you don’t have one.'),
      h('li', {}, h('div', { class: 'stack' },
        h('span', {}, 'On this phone, tap the button. On a computer, add an account in the app and type the key instead.'),
        h('div', { class: 'actions' }, h('a', { class: 'btn', href: link }, 'Add to authenticator app')),
        h('p', {}, 'Key: ', h('code', { class: 'secret-key', 'aria-label': `Key: ${secret.split('').join(' ')}` }, grouped)))),
      h('li', {}, 'Type the code the app shows.')),
    form,
  ];
}

function showBackupCodes(ctx, codes) {
  const save = actionButton('Download codes', () => {
    const text = `Mining Match backup codes for ${ctx.me.email}\nEach code works once.\n\n${codes.join('\n')}\n`;
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
    const a = h('a', { href: url, download: 'mining-match-backup-codes.txt' });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  });
  const done = actionButton('I’ve saved them', async () => {
    await ctx.refresh();
    if (ctx.me && ctx.me.role === 'admin') ctx.go('/admin');
    else ctx.reload('#tf-title');
  }, { class: 'btn primary' });
  return [
    h('p', { class: 'notice ok', role: 'status' }, 'Two-factor login is on. Other devices were logged out.'),
    h('p', {}, h('strong', {}, 'Save these backup codes now. '), 'If you lose your phone, each one gets you in once. '
      + 'They won’t be shown again. Keep them somewhere safe, away from your phone.'),
    h('ul', { class: 'backup-codes', 'aria-label': 'Backup codes' }, codes.map((c) => h('li', {}, h('code', {}, c)))),
    h('div', { class: 'actions' }, save, done),
  ];
}

/** A small form behind a button, needing the password and a code. */
function passwordAndCode(label, hint, run, buttonClass = 'btn') {
  const wrap = h('div', { class: 'stack' });
  const open = h('button', { type: 'button', class: buttonClass, 'aria-expanded': 'false' }, label);
  open.addEventListener('click', () => {
    const password = passwordBox();
    const code = codeBox();
    const error = errorArea();
    const form = onSubmit(h('form', { class: 'stack sub-form' },
      h('p', { class: 'small' }, hint), field('Your password', password), field('Code', code, CODE_HINT), error.el,
      h('div', { class: 'actions' }, h('button', { type: 'submit', class: buttonClass }, label),
        h('button', { type: 'button', class: 'btn', onclick: () => { form.remove(); open.hidden = false; open.focus(); } }, 'Cancel'))),
    error, () => run(password.value, code.value));
    open.hidden = true;
    wrap.append(form);
    password.focus();
  });
  wrap.append(open);
  return wrap;
}
