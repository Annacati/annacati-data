// annacati-data — the sellable GTFS extract API (data.annacati.com).
//
// Never transforms and never touches large data uncontrolled: the value-add
// transform is pre-baked into R2 by baker/bake_curated.py; here the two modes are
// both cheap by construction — FULL is an R2 passthrough of the curated feed,
// SAMPLE is segmentation of a small slice (N complete routes/agency). Auth,
// entitlement seam, and Analytics Engine metering are wired from day one so paid
// tiers are later config, not a rebuild.

import { authorize } from "./auth";
import { segmentZip } from "./segment";
import { buildBundle, FeedEntry, ManifestInput } from "./bundle";

export interface Env {
  DATA: R2Bucket;
  ANALYTICS?: AnalyticsEngineDataset;
  // Consumer entitlement Worker (app-user Pro). Declared for reuse; NOT the
  // data-API gate — see auth.ts. Optional so a dev deploy without it still runs.
  ENTITLEMENT?: Fetcher;
  DATA_API_ADMIN_TOKEN: string;
}

interface CatalogFeed {
  slug: string;
  name: string;
  routes: number;
  trips: number;
  stops: number;
  service_start: string;
  service_end: string;
  bytes: number;
  transforms?: Record<string, number>;
}

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    try {
      if (url.pathname === "/health") return json({ ok: true });
      if (url.pathname === "/catalog") return await handleCatalog(request, env);
      if (url.pathname === "/extract") return await handleExtract(request, env, ctx);
      return json({ error: "not found" }, 404);
    } catch (e) {
      return json({ error: "internal error", detail: String(e) }, 500);
    }
  },
};

async function loadIndex(env: Env): Promise<{ feeds: CatalogFeed[] } | null> {
  const obj = await env.DATA.get("curated/index.json");
  if (!obj) return null;
  return JSON.parse(await obj.text());
}

async function handleCatalog(request: Request, env: Env): Promise<Response> {
  const auth = authorize(request, env);
  if (!auth.ok) return json({ error: auth.error }, auth.status);
  const idx = await loadIndex(env);
  if (!idx) return json({ error: "catalogue not baked yet" }, 503);
  return json({
    feeds: idx.feeds.map((f) => ({
      slug: f.slug,
      name: f.name,
      routes: f.routes,
      trips: f.trips,
      stops: f.stops,
      service_start: f.service_start,
      service_end: f.service_end,
    })),
  });
}

// route_ids param form: "slug:idA|idB;slug2:idC" -> { slug: [idA,idB], ... }
function parseRouteIds(raw: string | null): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  if (!raw) return out;
  for (const part of raw.split(";")) {
    const [slug, ids] = part.split(":");
    if (slug && ids) out[slug.trim()] = ids.split("|").map((s) => s.trim()).filter(Boolean);
  }
  return out;
}

async function handleExtract(
  request: Request,
  env: Env,
  ctx: ExecutionContext
): Promise<Response> {
  const auth = authorize(request, env);
  if (!auth.ok) return json({ error: auth.error }, auth.status);

  const p = new URL(request.url).searchParams;
  const agencies = (p.get("agencies") || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (!agencies.length) return json({ error: "agencies is required (comma-separated slugs)" }, 400);

  const mode = (p.get("mode") || "sample") === "full" ? "full" : "sample";
  if (mode === "full" && !auth.limits.allowFull)
    return json({ error: "full mode not permitted for this plan" }, 403);
  if (agencies.length > auth.limits.maxAgencies)
    return json({ error: `too many agencies (limit ${auth.limits.maxAgencies})` }, 403);

  const routesPerAgency = Math.max(1, parseInt(p.get("routes_per_agency") || "3", 10) || 3);
  const routeIds = parseRouteIds(p.get("route_ids"));

  const idx = await loadIndex(env);
  if (!idx) return json({ error: "catalogue not baked yet" }, 503);
  const bySlug = new Map(idx.feeds.map((f) => [f.slug, f]));

  const unknown = agencies.filter((s) => !bySlug.has(s));
  if (unknown.length) return json({ error: `unknown agency slug(s): ${unknown.join(", ")}` }, 400);

  const requestId = crypto.randomUUID();
  const date = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  const feedVersion = `annacati-${requestId.slice(0, 8)}`;

  const feedZips: Record<string, Uint8Array> = {};
  const manifestFeeds: FeedEntry[] = [];
  let totalBytes = 0;

  for (const slug of agencies) {
    const meta = bySlug.get(slug)!;
    const obj = await env.DATA.get(`curated/${slug}.zip`);
    if (!obj) return json({ error: `curated feed missing for ${slug}` }, 503);
    const curated = new Uint8Array(await obj.arrayBuffer());

    let feedBytes: Uint8Array;
    let routes = meta.routes;
    let trips = meta.trips;
    let stops = meta.stops;

    if (mode === "full") {
      // Passthrough of the already-transformed curated feed, re-stamping the
      // watermark version only would require an unzip; keep it a true passthrough.
      feedBytes = curated;
    } else {
      const { zip, stats } = segmentZip(curated, {
        routesPerAgency,
        routeIds: routeIds[slug],
        feedVersion,
      });
      feedBytes = zip;
      routes = stats.routes;
      trips = stats.trips;
      stops = stats.stops;
    }

    feedZips[slug] = feedBytes;
    totalBytes += feedBytes.length;
    manifestFeeds.push({
      slug,
      name: meta.name,
      routes,
      trips,
      stops,
      service_start: meta.service_start,
      service_end: meta.service_end,
      bytes: feedBytes.length,
      transforms: meta.transforms,
    });
  }

  const manifest: ManifestInput = {
    mode,
    generatedAt: new Date().toISOString(),
    requestId,
    routesPerAgency: mode === "sample" ? routesPerAgency : undefined,
    feeds: manifestFeeds,
  };
  const bundle = buildBundle(manifest, feedZips);

  meter(env, {
    keyId: auth.keyId,
    mode,
    agencies,
    routeCount: manifestFeeds.reduce((n, f) => n + f.routes, 0),
    bytes: bundle.length,
    requestId,
  });

  const filename = `annacati-${mode}-${date}.zip`;
  return new Response(bundle, {
    status: 200,
    headers: {
      "content-type": "application/zip",
      "content-disposition": `attachment; filename="${filename}"`,
      "x-request-id": requestId,
    },
  });
}

function meter(
  env: Env,
  d: { keyId: string; mode: string; agencies: string[]; routeCount: number; bytes: number; requestId: string }
): void {
  if (!env.ANALYTICS) return;
  try {
    env.ANALYTICS.writeDataPoint({
      blobs: [d.keyId, d.mode, d.agencies.join(","), d.requestId],
      doubles: [d.bytes, d.agencies.length, d.routeCount],
      indexes: [d.keyId],
    });
  } catch {
    // metering must never fail a request
  }
}
