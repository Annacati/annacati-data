// Assemble the download the operator emails: a bundle zip of per-agency GTFS
// feeds + a MANIFEST.json + a human README. Each feed stays a clean standard
// GTFS zip so a buyer can drop any one straight into their tools.

import { zipSync } from "fflate";

export interface FeedEntry {
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

export interface ManifestInput {
  mode: "sample" | "full";
  generatedAt: string;
  requestId: string;
  routesPerAgency?: number;
  feeds: FeedEntry[];
}

export function buildManifest(m: ManifestInput): object {
  return {
    product: "Annacati curated GTFS",
    mode: m.mode,
    generated_at: m.generatedAt,
    request_id: m.requestId,
    sourcing: "self-hosted (feeds Annacati scrapes and builds)",
    sample_note:
      m.mode === "sample"
        ? `Representative SAMPLE: the ${m.routesPerAgency ?? 3} busiest complete route(s) per ` +
          `agency (all their trips, all calendar days, full shapes). Not the full network.`
        : "Full dataset for the listed agencies.",
    value_add: {
      curation:
        "Canonical stop names, repositioned coordinates, and multilingual stop " +
        "names (translations.txt), applied from our curation database.",
      lua_edits:
        "Per-operator brand route colours (WCAG-AA text contrast), tidied route " +
        "short names, coach route_type, rewritten trip headsigns, and agency renames.",
      shapes: "pfaedle map-matched shapes.txt where available.",
      merges_note:
        "Cross-operator hub merges are a routing-engine behaviour (same-named stops " +
        "within ~300 m are linked at query time), not encoded in feed bytes; the " +
        "curated feeds carry the unified names + coordinates that make that happen.",
    },
    realtime_note:
      "GTFS-RT (TripUpdates / Service Alerts) is available separately and is not " +
      "part of this static bundle.",
    license:
      "SAMPLE FOR EVALUATION ONLY. Not licensed for redistribution or production " +
      "use. Contact Annacati for licensing terms.",
    contact: "hello@annacati.com",
    feeds: m.feeds.map((f) => ({
      slug: f.slug,
      name: f.name,
      file: `feeds/${f.slug}.zip`,
      routes: f.routes,
      trips: f.trips,
      stops: f.stops,
      service_start: f.service_start,
      service_end: f.service_end,
      bytes: f.bytes,
      transforms: f.transforms,
    })),
  };
}

// README in italiano, autonomo (non riusa le stringhe inglesi del MANIFEST).
function readme(m: ManifestInput, manifest: any): string {
  const lines: string[] = [];
  const nota =
    m.mode === "sample"
      ? `Campione rappresentativo: le ${m.routesPerAgency ?? 3} linee complete più ` +
        `trafficate per operatore (tutte le corse, tutti i giorni di calendario, ` +
        `tracciati completi). Non è la rete completa.`
      : "Set di dati completo per gli operatori elencati.";
  lines.push("# GTFS curato Annacati " + (m.mode === "sample" ? "(campione)" : "(dataset)"));
  lines.push("");
  lines.push(nota);
  lines.push("");
  lines.push("## Feed inclusi");
  lines.push("");
  lines.push("| Operatore | File | Linee | Corse | Fermate | Periodo di servizio |");
  lines.push("|---|---|---:|---:|---:|---|");
  for (const f of m.feeds) {
    lines.push(
      `| ${f.name} | feeds/${f.slug}.zip | ${f.routes} | ${f.trips} | ${f.stops} | ` +
        `${f.service_start}–${f.service_end} |`
    );
  }
  lines.push("");
  lines.push("## Tempo reale");
  lines.push("");
  lines.push(
    "Il GTFS-RT (aggiornamenti delle corse / avvisi di servizio) è disponibile " +
      "separatamente e non fa parte di questo pacchetto statico."
  );
  lines.push("");
  lines.push("## Licenza");
  lines.push("");
  lines.push(
    "CAMPIONE FORNITO SOLO A SCOPO DI VALUTAZIONE. Non concesso in licenza per la " +
      "ridistribuzione o l'uso in produzione. Per le condizioni di licenza " +
      "contattare Annacati: " + manifest.contact
  );
  lines.push("");
  return lines.join("\n");
}

export function buildBundle(
  m: ManifestInput,
  feedZips: Record<string, Uint8Array> // slug -> per-agency gtfs zip bytes
): Uint8Array {
  const manifest = buildManifest(m);
  const enc = new TextEncoder();
  const files: Record<string, Uint8Array> = {
    "MANIFEST.json": enc.encode(JSON.stringify(manifest, null, 2)),
    "README.md": enc.encode(readme(m, manifest)),
  };
  for (const [slug, bytes] of Object.entries(feedZips)) {
    files[`feeds/${slug}.zip`] = bytes;
  }
  // Per-agency zips are already deflated; store them (level 0) to avoid double
  // compression, but let the text files compress.
  return zipSync(files, { level: 6 });
}
