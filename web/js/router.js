// Screens are addressed after "#", e.g. #/jobs/abc123?tab=open. Anything after # never
// reaches a server, which keeps tokens in emailed links out of server logs.

const routes = [];

export function route(pattern, view, options = {}) {
  const keys = [];
  const regex = new RegExp(`^${pattern.replace(/:(\w+)/g, (_, key) => { keys.push(key); return '([^/]+)'; })}$`);
  routes.push({ pattern, regex, keys, view, ...options });
}

export function parse(hash = location.hash) {
  const raw = hash.startsWith('#/') ? hash.slice(1) : '/';
  const [path, search = ''] = raw.split('?');
  const query = Object.fromEntries(new URLSearchParams(search));
  for (const r of routes) {
    const m = r.regex.exec(path);
    if (m) {
      const params = {};
      r.keys.forEach((key, i) => { params[key] = decodeURIComponent(m[i + 1]); });
      return { route: r, path, params, query };
    }
  }
  return { route: null, path, params: {}, query };
}

export function go(path) {
  if (location.hash === `#${path}`) {
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  } else {
    location.hash = path;
  }
}

/** Replace the current address without adding to history (e.g. after using an emailed link). */
export function replace(path) {
  history.replaceState(null, '', `#${path}`);
}
