// JSON API client: same-origin cookies, typed errors, login redirect on 401.

export class ApiError extends Error {
  constructor(message, status = 0, data = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

let redirecting = false;
function redirectToLogin() {
  if (redirecting || location.pathname === "/login") return;
  redirecting = true;
  location.href = `/login?next=${encodeURIComponent(location.pathname + location.search)}`;
}

async function request(method, path, body, { signal, timeout = 20000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new DOMException("timeout", "TimeoutError")), timeout);
  const onAbort = () => controller.abort(signal.reason);
  if (signal) {
    if (signal.aborted) controller.abort(signal.reason);
    else signal.addEventListener("abort", onAbort, { once: true });
  }
  const options = { method, credentials: "same-origin", headers: { Accept: "application/json" }, signal: controller.signal };
  if (method !== "GET") {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body ?? {});
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (err) {
    if (signal?.aborted) throw err;
    if (err?.name === "TimeoutError" || controller.signal.reason?.name === "TimeoutError") {
      throw new ApiError("The server took too long to respond", 0);
    }
    throw new ApiError("Cannot reach the terminal server — is it running?", 0);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  }
  let data = null;
  try { data = await response.json(); } catch { data = null; }
  if (response.status === 401) {
    redirectToLogin();
    throw new ApiError("Your session expired — please sign in again", 401, data);
  }
  if (!response.ok) {
    throw new ApiError(data?.error || `Request failed (HTTP ${response.status})`, response.status, data);
  }
  return data;
}

export const api = {
  get: (path, options) => request("GET", path, null, options),
  post: (path, body, options) => request("POST", path, body, options),
};

export function qs(params = {}) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") search.set(key, value);
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}
