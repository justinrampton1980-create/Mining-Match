// Talking to the Mining Match API. The session lives in an HttpOnly cookie that page
// scripts can't read; every request carries X-Requested-With so the server accepts it.

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

const HEADERS = { 'X-Requested-With': 'mining-match' };

function describe(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => {
      const where = (d.loc || []).filter((p) => p !== 'body' && p !== 'query').join(' ');
      return where ? `${where.replaceAll('_', ' ')}: ${d.msg}` : d.msg;
    }).join('; ');
  }
  return 'Something went wrong. Try again.';
}

export async function api(method, path, body, { form } = {}) {
  const options = { method, headers: { ...HEADERS }, credentials: 'same-origin' };
  if (form) {
    options.body = form;
  } else if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new ApiError(0, 'Can’t reach Mining Match. Check your connection and try again.');
  }
  if (response.status === 204) return null;
  const type = response.headers.get('content-type') || '';
  const data = type.includes('application/json') ? await response.json() : await response.blob();
  if (!response.ok) {
    let message = data && data.detail !== undefined ? describe(data.detail) : 'Something went wrong. Try again.';
    if (response.status === 429) message = message || 'Too many attempts. Wait a few minutes and try again.';
    const error = new ApiError(response.status, message);
    if (response.status === 401 && !path.startsWith('/auth/') && path !== '/me') {
      // The session expired or was ended elsewhere: the app takes the person to log in.
      error.sessionEnded = true;
      window.dispatchEvent(new CustomEvent('mm:logged-out'));
    }
    throw error;
  }
  return data;
}

export const get = (path) => api('GET', path);
export const post = (path, body) => api('POST', path, body ?? {});
export const put = (path, body) => api('PUT', path, body ?? {});
export const patch = (path, body) => api('PATCH', path, body ?? {});
export const del = (path) => api('DELETE', path);

export function query(params) {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') q.set(key, value);
  }
  const s = q.toString();
  return s ? `?${s}` : '';
}

// Files are fetched with the session, then handed to the browser as a download.
export async function download(path, filename) {
  const blob = await api('GET', path);
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
