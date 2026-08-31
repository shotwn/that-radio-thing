/** Browser client for the versioned ThatRadioThing administration API. */

const configuredBase = window.__TRT_ADMIN_API_BASE__ || "/api/admin/v1";

export const API_BASE = configuredBase.replace(/\/$/, "");

/** Return a named cookie value, decoding standard percent escapes. */
export function cookieValue(name) {
  const prefix = `${name}=`;
  const item = document.cookie
    .split(";")
    .map((value) => value.trim())
    .find((value) => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : "";
}

/** Perform one API request and normalize both JSON and plain-text failures. */
export async function request(path, options = {}) {
  const headers = {
    ...(options.body ? { "Content-Type": "application/json" } : {}),
    ...(options.headers || {}),
  };
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    cache: "no-store",
    ...options,
    headers,
  });
  const text = await response.text();
  let payload;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = { message: text };
  }
  if (!response.ok) {
    const error = new Error(
      payload?.message || `Request failed (${response.status})`,
    );
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

/** Send one state-changing request with CSRF and retry-deduplication headers. */
export function mutate(path, method, body, csrf) {
  return request(path, {
    method,
    body: body === undefined ? undefined : JSON.stringify(body),
    headers: {
      "X-CSRF-Token": csrf || cookieValue("trt_csrf"),
      "X-Request-ID": crypto.randomUUID(),
    },
  });
}
