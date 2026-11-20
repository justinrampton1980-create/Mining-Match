// Building the page. Everything goes through h(), which sets text with text nodes and never
// parses HTML, so names and notes typed by users can't inject script.

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith('on') && typeof value === 'function') {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'class') {
      el.className = value;
    } else if (key === 'vars') {
      for (const [name, v] of Object.entries(value)) el.style.setProperty(name, v);
    } else if (key === 'value' || key === 'checked' || key === 'selected') {
      el[key] = value;
    } else if (value === true) {
      el.setAttribute(key, '');
    } else {
      el.setAttribute(key, String(value));
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

let fieldCount = 0;

/** A labelled control. hint is shown under the label and linked for screen readers. */
export function field(label, control, hint) {
  const id = control.id || `f${++fieldCount}`;
  control.id = id;
  let hintEl = null;
  if (hint) {
    const hintId = `${id}-hint`;
    control.setAttribute('aria-describedby', hintId);
    hintEl = h('span', { class: 'hint', id: hintId }, hint);
  }
  return h('div', { class: 'field' }, h('label', { for: id }, label), hintEl, control);
}

export function input(props = {}) {
  return h('input', { type: 'text', ...props });
}

export function select(options, value, props = {}) {
  return h('select', props, options.map(([v, label]) => h('option', { value: v, selected: v === value }, label)));
}

/** A checkbox and its label. hint (optional) goes under the label, linked for screen readers. */
export function checkbox(label, checked, props = {}, hint = null) {
  const box = h('input', { type: 'checkbox', checked, ...props });
  const el = h('label', { class: 'check' }, box, h('span', {}, label));
  if (!hint) return { box, el };
  const hintId = `f${++fieldCount}-hint`;
  box.setAttribute('aria-describedby', hintId);
  return { box, el: h('div', { class: 'check-field' }, el, h('span', { class: 'hint', id: hintId }, hint)) };
}

export function datalist(id, names) {
  return h('datalist', { id }, names.map((n) => h('option', { value: n })));
}

/** An error message area for a form. */
export function errorArea() {
  const el = h('p', { class: 'form-error', role: 'alert' });
  return {
    el,
    show(err) { el.textContent = err && err.message ? err.message : String(err); },
    clear() { el.textContent = ''; },
  };
}

/**
 * Wire up a form: disables its submit button while working, shows errors in place.
 * handler receives the event's form and may be async.
 */
export function onSubmit(form, error, handler) {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    error.clear();
    if (button) button.disabled = true;
    try {
      await handler(form);
    } catch (err) {
      if (err && err.sessionEnded) return; // the app takes people to log in
      error.show(err);
    } finally {
      if (button && button.isConnected) button.disabled = false;
    }
  });
  return form;
}

/** A button for an async action; shows a toast if it fails. */
export function actionButton(label, run, props = {}) {
  const button = h('button', { type: 'button', class: 'btn', ...props }, label);
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await run(button);
    } catch (err) {
      if (!err || !err.sessionEnded) toast(err.message || String(err), 'bad');
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  });
  return button;
}

/** For things that can't be undone: the first tap arms the button, the second acts. */
export function confirmButton(label, armedLabel, run, props = {}) {
  let armed = false;
  let timer;
  const button = h('button', { type: 'button', class: 'btn danger', ...props }, label);
  button.addEventListener('click', async () => {
    if (!armed) {
      armed = true;
      button.textContent = armedLabel;
      button.classList.add('armed');
      timer = setTimeout(() => {
        armed = false;
        button.textContent = label;
        button.classList.remove('armed');
      }, 4000);
      return;
    }
    clearTimeout(timer);
    button.disabled = true;
    try {
      await run();
    } catch (err) {
      if (!err || !err.sessionEnded) toast(err.message || String(err), 'bad');
      armed = false;
      button.textContent = label;
      button.classList.remove('armed');
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  });
  return button;
}

let toastTimer;
export function toast(message, kind = '') {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 3500);
}

/** Collapsible add-form: a button that reveals a small form, and hides it after saving. */
export function reveal(buttonLabel, buildForm) {
  const wrap = h('div', { class: 'stack' });
  const button = h('button', { type: 'button', class: 'btn', 'aria-expanded': 'false' }, buttonLabel);
  const show = () => {
    const form = buildForm(close);
    wrap.replaceChildren(form);
    const first = form.querySelector('input, select, textarea');
    if (first) first.focus();
  };
  const close = () => {
    wrap.replaceChildren(button);
    button.focus();
  };
  button.addEventListener('click', show);
  wrap.append(button);
  return wrap;
}
