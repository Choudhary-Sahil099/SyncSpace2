export type User = { id: string; username: string };
export type DocSummary = {
  id: string;
  title: string;
  revision: number;
  updated_at: string;
  owner: boolean;
};
export type Doc = DocSummary & {
  content: string;
  role: "owner" | "editor" | "viewer";
  owner_id: string;
};

const HOST = window.location.hostname;

const BASE =
  import.meta.env.VITE_API_URL || `http://${HOST}:8000`;

export const WS_BASE =
  import.meta.env.VITE_WS_URL || `ws://${HOST}:8000`;

export const getToken = () =>
  sessionStorage.getItem("syncspace_token");

export const setToken = (token: string | null) =>
  token
    ? sessionStorage.setItem("syncspace_token", token)
    : sessionStorage.removeItem("syncspace_token");
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `Request failed: ${res.status}`;
    try {
      const x = await res.json();
      detail = x.detail || detail;
    } catch {}
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}
