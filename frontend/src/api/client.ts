const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";
const API_KEY = import.meta.env.VITE_API_KEY ?? "";

export class ApiFetchError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(`${status}: ${detail}`);
    this.name = "ApiFetchError";
    this.status = status;
    this.detail = detail;
  }
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-API-Key", API_KEY);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${BASE_URL}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // non-JSON body; keep statusText
    }
    throw new ApiFetchError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
