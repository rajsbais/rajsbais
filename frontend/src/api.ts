export type Json = any;

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) || "/api/v1";

export function getToken(): string | null {
  try { return localStorage.getItem("sdtf.token"); } catch { return null; }
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

export async function api<T = Json>(path: string, opts: { method?: string; body?: Json; params?: Record<string, any> } = {}): Promise<T> {
  const url = new URL(BASE + path, window.location.origin);
  if (opts.params) Object.entries(opts.params).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v)); });
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const tok = getToken();
  if (tok) headers.Authorization = `Bearer ${tok}`;
  const res = await fetch(url.toString(), { method: opts.method || (opts.body ? "POST" : "GET"), headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (res.status === 401) { try { localStorage.removeItem("sdtf.token"); } catch {} }
  const text = await res.text();
  let data: Json = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) throw new ApiError(res.status, (data && data.detail) ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText);
  return data as T;
}

export async function login(username: string, password: string) {
  const r = await api<{ access_token: string; username: string; roles: string[]; display_name: string }>("/auth/token", { body: { username, password } });
  localStorage.setItem("sdtf.token", r.access_token);
  localStorage.setItem("sdtf.user", JSON.stringify({ username: r.username, roles: r.roles, display_name: r.display_name }));
  return r;
}

export function currentUser(): { username: string; roles: string[]; display_name: string } | null {
  try { const s = localStorage.getItem("sdtf.user"); return s ? JSON.parse(s) : null; } catch { return null; }
}

export function logout() { localStorage.removeItem("sdtf.token"); localStorage.removeItem("sdtf.user"); }

export function fmtBytes(n: number | undefined): string {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function fmtNum(n: number | string | undefined | null): string {
  if (n === undefined || n === null || n === "") return "-";
  const v = typeof n === "string" ? Number(n) : n;
  if (Number.isNaN(v)) return String(n);
  return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}
