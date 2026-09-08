// Authorization seam. v1 gates on a single admin Bearer (the operator pulling
// sample/full bundles to email). It is written as a seam that returns a
// {plan, limits} verdict so paid tiers later are a change here, not a rewrite.
//
// NOTE on identity: a data-API *customer* is an API KEY, not a consumer app-user,
// so the eventual plug is most likely a per-key registry (KV/DO) mapping key ->
// {plan, limits}. The consumer entitlement Worker (env.ENTITLEMENT.isPro over an
// app-user id) gates the iOS/Android Pro features, a different identity space —
// the binding is declared for reuse but is NOT the data-API gate. See the wiki
// handoff. Keep all future plan logic behind authorize().

import type { Env } from "./index";

export interface AuthResult {
  ok: boolean;
  status?: number;
  error?: string;
  keyId: string;
  plan: string;
  limits: { maxAgencies: number; allowFull: boolean };
}

const DENY = (status: number, error: string): AuthResult => ({
  ok: false,
  status,
  error,
  keyId: "",
  plan: "none",
  limits: { maxAgencies: 0, allowFull: false },
});

export function authorize(request: Request, env: Env): AuthResult {
  const auth = request.headers.get("Authorization") || "";
  const m = auth.match(/^Bearer\s+(.+)$/i);
  const token = m ? m[1].trim() : "";
  if (!token) return DENY(401, "missing bearer token");
  if (!env.DATA_API_ADMIN_TOKEN) return DENY(500, "server not configured");
  // Constant-time-ish compare (tokens are short; avoid early-exit length leak).
  if (!safeEqual(token, env.DATA_API_ADMIN_TOKEN)) return DENY(403, "invalid token");
  // Admin: all-access. Future tiers return narrower limits from a key registry.
  return {
    ok: true,
    keyId: "admin",
    plan: "admin",
    limits: { maxAgencies: 1000, allowFull: true },
  };
}

function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
