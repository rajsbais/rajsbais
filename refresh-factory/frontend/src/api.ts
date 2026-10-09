// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type J = any;

let currentUser = "alice.basis";
export const setUser = (u: string) => { currentUser = u; };
export const getUser = () => currentUser;

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function raw(path: string, init: RequestInit = {}): Promise<Response> {
  const r = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", "X-Demo-User": currentUser, ...(init.headers || {}) },
  });
  if (!r.ok) {
    let msg = r.statusText;
    try { const b = await r.json(); msg = typeof b.detail === "string" ? b.detail : JSON.stringify(b.detail); } catch { /* keep statusText */ }
    throw new ApiError(r.status, msg);
  }
  return r;
}

export const api = {
  get: async (p: string): Promise<J> => (await raw(p)).json(),
  text: async (p: string): Promise<string> => (await raw(p)).text(),
  post: async (p: string, body?: unknown): Promise<J> => (await raw(p, { method: "POST", body: JSON.stringify(body ?? {}) })).json(),
  put: async (p: string, body?: unknown): Promise<J> => (await raw(p, { method: "PUT", body: JSON.stringify(body ?? {}) })).json(),
  download: async (p: string, name: string) => {
    const blob = await (await raw(p)).blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name; a.click();
    URL.revokeObjectURL(a.href);
  },
};

export const store = {
  get: (k: string): string | null => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k: string, v: string) => { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};
