// Sample-mode segmentation: keep N COMPLETE routes per agency (all their trips,
// all calendar days, full shapes) — a genuine slice, not a thin toy. Runs over an
// already-curated feed (the baker's gold zip), so every kept row carries our
// edits. Pure over the parsed tables; the same trips->stop_times->stops->
// calendar/shapes cascade the baker/gtfs.py checks use.

import { parseCsv, emitCsv, Table, Row } from "./csv";
import { unzipSync, zipSync, strToU8, strFromU8 } from "fflate";

export interface SegmentOpts {
  routesPerAgency: number;
  routeIds?: string[]; // explicit route_ids override the top-N pick
  feedVersion?: string; // watermark written into feed_info.txt
}

export interface SegmentStats {
  routes: number;
  trips: number;
  stops: number;
}

const T = {
  routes: "routes.txt",
  trips: "trips.txt",
  stopTimes: "stop_times.txt",
  stops: "stops.txt",
  calendar: "calendar.txt",
  calendarDates: "calendar_dates.txt",
  shapes: "shapes.txt",
  agency: "agency.txt",
  feedInfo: "feed_info.txt",
  translations: "translations.txt",
};

function pickRoutes(routes: Table, trips: Table, opts: SegmentOpts): Set<string> {
  if (opts.routeIds && opts.routeIds.length) {
    const wanted = new Set(opts.routeIds);
    return new Set(routes.rows.filter((r) => wanted.has(r.route_id)).map((r) => r.route_id));
  }
  const tripCount = new Map<string, number>();
  for (const t of trips.rows) {
    tripCount.set(t.route_id, (tripCount.get(t.route_id) ?? 0) + 1);
  }
  const ranked = routes.rows
    .map((r) => r.route_id)
    .sort((a, b) => {
      const d = (tripCount.get(b) ?? 0) - (tripCount.get(a) ?? 0);
      return d !== 0 ? d : a < b ? -1 : a > b ? 1 : 0; // deterministic tie-break
    });
  return new Set(ranked.slice(0, Math.max(1, opts.routesPerAgency)));
}

// Segment an already-unzipped feed (name -> bytes) and return the new file set.
export function segmentFeed(
  files: Record<string, Uint8Array>,
  opts: SegmentOpts
): { files: Record<string, Uint8Array>; stats: SegmentStats } {
  const read = (name: string): Table | null =>
    files[name] ? parseCsv(strFromU8(files[name])) : null;

  const routes = read(T.routes);
  const trips = read(T.trips);
  const stopTimes = read(T.stopTimes);
  const stops = read(T.stops);
  if (!routes || !trips || !stopTimes || !stops) {
    // Not a segmentable feed; return as-is rather than corrupting it.
    return { files, stats: { routes: routes?.rows.length ?? 0, trips: 0, stops: 0 } };
  }

  const keepRoutes = pickRoutes(routes, trips, opts);

  const keptTrips = trips.rows.filter((t) => keepRoutes.has(t.route_id));
  const keepTripIds = new Set(keptTrips.map((t) => t.trip_id));
  const keepServiceIds = new Set(keptTrips.map((t) => t.service_id).filter(Boolean));
  const keepShapeIds = new Set(keptTrips.map((t) => t.shape_id).filter(Boolean));

  const keptStopTimes = stopTimes.rows.filter((s) => keepTripIds.has(s.trip_id));
  const keepStopIds = new Set<string>();
  for (const s of keptStopTimes) keepStopIds.add(s.stop_id);

  // Pull in parent_station chains so a kept platform keeps its station.
  const stopById = new Map(stops.rows.map((s) => [s.stop_id, s]));
  const queue = [...keepStopIds];
  while (queue.length) {
    const id = queue.pop()!;
    const parent = stopById.get(id)?.parent_station;
    if (parent && !keepStopIds.has(parent)) {
      keepStopIds.add(parent);
      queue.push(parent);
    }
  }

  const keptRoutes = routes.rows.filter((r) => keepRoutes.has(r.route_id));
  const keepAgencyIds = new Set(keptRoutes.map((r) => r.agency_id).filter(Boolean));

  const out: Record<string, Uint8Array> = {};
  const put = (name: string, header: string[], rows: Row[]) => {
    out[name] = strToU8(emitCsv(rows, header));
  };

  put(T.routes, routes.header, keptRoutes);
  put(T.trips, trips.header, keptTrips);
  put(T.stopTimes, stopTimes.header, keptStopTimes);
  put(
    T.stops,
    stops.header,
    stops.rows.filter((s) => keepStopIds.has(s.stop_id))
  );

  const agency = read(T.agency);
  if (agency) {
    // If a feed has a single agency and routes omit agency_id, keep it all.
    const rows =
      keepAgencyIds.size === 0
        ? agency.rows
        : agency.rows.filter((a) => keepAgencyIds.has(a.agency_id));
    put(T.agency, agency.header, rows.length ? rows : agency.rows);
  }

  const cal = read(T.calendar);
  if (cal) put(T.calendar, cal.header, cal.rows.filter((c) => keepServiceIds.has(c.service_id)));
  const calD = read(T.calendarDates);
  if (calD)
    put(T.calendarDates, calD.header, calD.rows.filter((c) => keepServiceIds.has(c.service_id)));

  const shapes = read(T.shapes);
  if (shapes)
    put(T.shapes, shapes.header, shapes.rows.filter((s) => keepShapeIds.has(s.shape_id)));

  const tr = read(T.translations);
  if (tr) {
    put(
      T.translations,
      tr.header,
      tr.rows.filter((r) => r.table_name !== "stops" || keepStopIds.has(r.record_id))
    );
  }

  // feed_info: carry through, stamping the watermark version if given.
  const fi = read(T.feedInfo);
  if (fi) {
    if (opts.feedVersion) for (const r of fi.rows) r.feed_version = opts.feedVersion;
    put(T.feedInfo, fi.header, fi.rows);
  }

  return {
    files: out,
    stats: {
      routes: keptRoutes.length,
      trips: keptTrips.length,
      stops: keepStopIds.size,
    },
  };
}

// Convenience: unzip curated bytes, segment, re-zip.
export function segmentZip(
  zipBytes: Uint8Array,
  opts: SegmentOpts
): { zip: Uint8Array; stats: SegmentStats } {
  const files = unzipSync(zipBytes);
  const { files: segFiles, stats } = segmentFeed(files, opts);
  const zip = zipSync(segFiles, { level: 6 });
  return { zip, stats };
}
