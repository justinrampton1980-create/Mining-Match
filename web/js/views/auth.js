// Log in, sign up, confirm email, reset password.
import { h, field, input, select, errorArea, onSubmit, actionButton, toast } from '../dom.js';
import { post } from '../api.js';
import { ticket } from '../components.js';
import { STATES, today, isoDate } from '../format.js';

const PASSWORD_HINT = 'At least 10 characters. Avoid common passwords and your email address.';

function hero() {
  const valid = today(); valid.setFullYear(valid.getFullYear() + 4);
  const soon = today(); soon.setDate(soon.getDate() + 41);
  return h('section', { class: 'auth-hero' },
    h('h1', {}, 'Your tickets, matched to real jobs.'),
    h('p', { class: 'lede' },
      'Mining Match checks your licences against what each job needs. Employers see who can start. ',
      'You see what you qualify for, and your name and number stay hidden until you say yes.'),
    h('div', { class: 'tickets', 'aria-hidden': 'true' },
      ticket({ name: 'Standard 11', expiry: isoDate(valid) }),
      ticket({ name: 'Coal Board Medical', expiry: isoDate(soon) })));
}

export async function login(ctx) {
  const email = input({ type: 'email', autocomplete: 'email', required: true, name: 'email' });
  const password = input({ type: 'password', autocomplete: 'current-password', required: true, name: 'password' });
  const error = errorArea();
  const resend = h('p', { class: 'small' });
  const form = onSubmit(h('form', { class: 'stack' },
    field('Email', email), field('Password', password), error.el, resend,
    h('button', { type: 'submit', class: 'btn primary' }, 'Log in')), error, async () => {
    resend.replaceChildren();
    try {
      await post('/auth/login', { email: email.value, password: password.value, use_cookie: true });
    } catch (err) {
      if (err.status === 403 && /confirm/i.test(err.message)) {
        resend.replaceChildren(h('a', { href: `#/check-email?email=${encodeURIComponent(email.value)}` }, 'Send a new confirmation link'));
      }
      throw err;
    }
    await ctx.refresh();
    ctx.go(ctx.home());
  });
  return h('div', { class: 'auth' }, hero(),
    h('section', { class: 'panel stack', 'aria-labelledby': 'login-title' },
      h('h2', { id: 'login-title' }, 'Log in'), form,
      h('p', { class: 'small' }, h('a', { href: '#/forgot-password' }, 'Forgot your password?')),
      h('p', { class: 'small' }, 'New to Mining Match? ', h('a', { href: '#/signup' }, 'Create an account'))));
}

export async function signup(ctx) {
  let role = ctx.query.role === 'employer' ? 'employer' : 'candidate';
  const email = input({ type: 'email', autocomplete: 'email', required: true });
  const password = input({ type: 'password', autocomplete: 'new-password', required: true, minlength: '10' });
  const name = input({ autocomplete: 'name', required: true });
  const company = input({ autocomplete: 'organization', required: true });
  const contact = input({ autocomplete: 'name' });
  const phone = input({ type: 'tel', autocomplete: 'tel' });
  const state = select(STATES, '');
  const roleFields = h('div', { class: 'stack' });
  const drawRole = () => {
    roleFields.replaceChildren(...(role === 'candidate'
      ? [field('Full name', name), field('State you live in', state, 'Used to match you with jobs in your state.'),
        field('Mobile number (optional)', phone, 'Only shared with employers you approve.')]
      : [field('Company name', company), field('Your name (optional)', contact),
        field('Phone (optional)', phone), field('State', state)]));
    name.required = role === 'candidate';
    company.required = role === 'employer';
  };
  const option = (value, label) => h('label', { class: 'choice' },
    h('input', { type: 'radio', name: 'role', value, checked: role === value, onchange: () => { role = value; drawRole(); } }), label);
  drawRole();

  const error = errorArea();
  const form = onSubmit(h('form', { class: 'stack' },
    h('fieldset', { class: 'choices' }, h('legend', { class: 'visually-hidden' }, 'Account type'),
      option('candidate', 'I’m looking for work'), option('employer', 'I’m hiring')),
    roleFields, field('Email', email), field('Password', password, PASSWORD_HINT),
    error.el, h('button', { type: 'submit', class: 'btn primary' }, 'Create account')), error, async () => {
    const body = { email: email.value, password: password.value };
    if (role === 'candidate') Object.assign(body, { name: name.value, location: state.value || null, phone: phone.value || null });
    else Object.assign(body, { company_name: company.value, contact_name: contact.value || null, phone: phone.value || null, location: state.value || null });
    await post(`/auth/register/${role}`, body);
    ctx.go(`/check-email?email=${encodeURIComponent(email.value)}`);
  });
  return h('div', { class: 'auth' },
    h('section', { class: 'auth-hero' }, h('h1', {}, 'Create your account'),
      h('p', { class: 'lede' }, 'Candidates join free. Employers pay only when they hire someone they found here.')),
    h('section', { class: 'panel stack' }, form,
      h('p', { class: 'small' }, 'Already have an account? ', h('a', { href: '#/login' }, 'Log in'))));
}

export async function checkEmail(ctx) {
  const email = ctx.query.email || '';
  const address = input({ type: 'email', value: email, autocomplete: 'email' });
  return h('div', { class: 'stack' },
    h('h1', {}, 'Check your email'),
    h('p', { class: 'lede' }, email
      ? `A confirmation link is on its way to ${email}. Open it to finish setting up your account.`
      : 'Open the confirmation link we emailed you to finish setting up your account.'),
    h('p', {}, 'The link works for 48 hours. If it hasn’t arrived in a few minutes, check your junk folder or send a new one.'),
    h('div', { class: 'panel stack' }, field('Email', address),
      h('div', { class: 'actions' }, actionButton('Send a new link', async () => {
        await post('/auth/verify-email/resend', { email: address.value });
        toast('Sent. Check your inbox.');
      }))),
    h('p', {}, h('a', { href: '#/login' }, 'Back to log in')));
}

export async function verifyEmail(ctx) {
  const token = ctx.query.token;
  ctx.replace('/verify-email'); // keep the token out of the address bar and history
  try {
    if (!token) throw new Error('This link is incomplete. Open the link from your email again.');
    await post('/auth/verify-email', { token });
    return h('div', { class: 'stack' }, h('h1', {}, 'Email confirmed'),
      h('p', { class: 'lede' }, 'Your account is ready.'),
      h('p', {}, h('a', { class: 'btn primary', href: ctx.me ? `#${ctx.home()}` : '#/login' }, ctx.me ? 'Continue' : 'Log in')));
  } catch (err) {
    return h('div', { class: 'stack' }, h('h1', {}, 'That link didn’t work'),
      h('p', { class: 'lede' }, err.message),
      h('p', {}, h('a', { href: '#/check-email' }, 'Send a new confirmation link')));
  }
}

export async function unsubscribe(ctx) {
  const token = ctx.query.token;
  ctx.replace('/unsubscribe'); // keep the token out of the address bar and history
  try {
    if (!token) throw new Error('This link is incomplete. Open the link from your email again.');
    const result = await post('/reminders/unsubscribe', { token });
    return h('div', { class: 'stack' }, h('h1', {}, 'Reminders turned off'),
      h('p', { class: 'lede' }, result.detail),
      h('p', {}, 'Your licences and expiry dates are unchanged, and employers still see them. '
        + 'You can turn reminders back on under Your details in your profile.'),
      h('p', {}, h('a', { class: 'btn primary', href: ctx.me ? '#/profile' : '#/login' },
        ctx.me ? 'Go to your profile' : 'Log in')));
  } catch (err) {
    return h('div', { class: 'stack' }, h('h1', {}, 'That link didn’t work'),
      h('p', { class: 'lede' }, err.message),
      h('p', {}, h('a', { href: ctx.me ? '#/profile' : '#/login' },
        ctx.me ? 'Turn them off in your profile' : 'Log in to turn them off')));
  }
}

export async function forgot(ctx) {
  const email = input({ type: 'email', autocomplete: 'email', required: true });
  const error = errorArea();
  const wrap = h('div', { class: 'stack' });
  const form = onSubmit(h('form', { class: 'panel stack' },
    field('Email', email), error.el, h('button', { type: 'submit', class: 'btn primary' }, 'Send reset link')), error, async () => {
    await post('/auth/password-reset/request', { email: email.value });
    wrap.replaceChildren(h('h1', {}, 'Check your email'),
      h('p', { class: 'lede' }, `If there’s an account for ${email.value}, a reset link is on its way. It works for 1 hour.`),
      h('p', {}, h('a', { href: '#/login' }, 'Back to log in')));
  });
  wrap.append(h('h1', {}, 'Reset your password'),
    h('p', { class: 'lede' }, 'Enter your email and we’ll send you a link to choose a new password.'), form,
    h('p', {}, h('a', { href: '#/login' }, 'Back to log in')));
  return wrap;
}

export async function reset(ctx) {
  const token = ctx.query.token;
  ctx.replace('/reset-password');
  const password = input({ type: 'password', autocomplete: 'new-password', required: true, minlength: '10' });
  const again = input({ type: 'password', autocomplete: 'new-password', required: true });
  const error = errorArea();
  const wrap = h('div', { class: 'stack' });
  const form = onSubmit(h('form', { class: 'panel stack' },
    field('New password', password, PASSWORD_HINT), field('Type it again', again), error.el,
    h('button', { type: 'submit', class: 'btn primary' }, 'Save new password')), error, async () => {
    if (!token) throw new Error('This link is incomplete. Open the link from your email again.');
    if (password.value !== again.value) throw new Error('The two passwords don’t match.');
    await post('/auth/password-reset/confirm', { token, new_password: password.value });
    await ctx.refresh(); // every session ends on reset
    wrap.replaceChildren(h('h1', {}, 'Password saved'),
      h('p', { class: 'lede' }, 'You’ve been logged out everywhere. Log in with your new password.'),
      h('p', {}, h('a', { class: 'btn primary', href: '#/login' }, 'Log in')));
  });
  wrap.append(h('h1', {}, 'Choose a new password'), form);
  return wrap;
}
