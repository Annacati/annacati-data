import { describe, it, expect } from "vitest";
import { zipSync, unzipSync, strToU8, strFromU8 } from "fflate";
import { segmentFeed, segmentZip } from "./segment";
import { parseCsv } from "./csv";

// A synthetic curated feed: 3 routes with different trip counts, a parent
// station, two service ids, two shapes, and an en translation.
function fixture(): Record<string, Uint8Array> {
  const f: Record<string, string> = {
    "agency.txt": "agency_id,agency_name\nA1,Test Agency\n",
    "routes.txt":
      "route_id,agency_id,route_short_name,route_color,route_type\n" +
      "R1,A1,1,FF0000,3\nR2,A1,2,00FF00,3\nR3,A1,3,0000FF,3\n",
    "trips.txt":
      "route_id,service_id,trip_id,shape_id,trip_headsign\n" +
      "R1,WD,T1,S1,Centro\nR1,WD,T2,S1,Centro\nR1,WE,T3,S1,Centro\n" + // R1: 3 trips
      "R2,WD,T4,S2,Porto\nR2,WD,T5,S2,Porto\n" + // R2: 2 trips
      "R3,WD,T6,S2,Aero\n", // R3: 1 trip
    "stop_times.txt":
      "trip_id,stop_id,stop_sequence,departure_time,arrival_time\n" +
      "T1,ST1,1,08:00:00,08:00:00\nT1,ST2,2,08:10:00,08:10:00\n" +
      "T2,ST1,1,09:00:00,09:00:00\nT2,ST2,2,09:10:00,09:10:00\n" +
      "T3,ST1,1,10:00:00,10:00:00\nT3,ST2,2,10:10:00,10:10:00\n" +
      "T4,ST3,1,08:00:00,08:00:00\nT4,ST4,2,08:20:00,08:20:00\n" +
      "T5,ST3,1,09:00:00,09:00:00\nT5,ST4,2,09:20:00,09:20:00\n" +
      "T6,ST5,1,08:00:00,08:00:00\nT6,ST4,2,08:30:00,08:30:00\n",
    "stops.txt":
      "stop_id,stop_name,stop_lat,stop_lon,parent_station\n" +
      "ST1,Centro,37.5,15.0,STA\nST2,Borgo,37.6,15.1,\nST3,Porto,37.4,15.0,\n" +
      "ST4,Aero,37.3,14.9,\nST5,Nord,37.7,15.2,\nSTA,Centro Station,37.5,15.0,\n",
    "calendar.txt":
      "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n" +
      "WD,1,1,1,1,1,0,0,20260801,20261004\nWE,0,0,0,0,0,1,1,20260801,20261004\n",
    "calendar_dates.txt": "service_id,date,exception_type\nWD,20260815,2\nWE,20260815,1\n",
    "shapes.txt":
      "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n" +
      "S1,37.5,15.0,1\nS1,37.6,15.1,2\nS2,37.4,15.0,1\nS2,37.3,14.9,2\n",
    "feed_info.txt":
      "feed_publisher_name,feed_publisher_url,feed_lang,feed_version\nAnnacati,https://annacati.com,it,v0\n",
    "translations.txt":
      "table_name,field_name,language,translation,record_id\n" +
      "stops,stop_name,en,Downtown,ST1\nstops,stop_name,en,Harbour,ST3\n",
  };
  const out: Record<string, Uint8Array> = {};
  for (const [k, v] of Object.entries(f)) out[k] = strToU8(v);
  return out;
}

function tbl(files: Record<string, Uint8Array>, name: string) {
  return parseCsv(strFromU8(files[name]));
}

describe("segmentFeed", () => {
  it("keeps the top-N routes by trip count and cascades self-consistently", () => {
    const { files, stats } = segmentFeed(fixture(), { routesPerAgency: 2 });
    // R1 (3 trips) and R2 (2 trips) survive; R3 (1) dropped.
    const routeIds = tbl(files, "routes.txt").rows.map((r) => r.route_id).sort();
    expect(routeIds).toEqual(["R1", "R2"]);
    expect(stats.routes).toBe(2);

    const tripIds = new Set(tbl(files, "trips.txt").rows.map((r) => r.trip_id));
    expect([...tripIds].sort()).toEqual(["T1", "T2", "T3", "T4", "T5"]);

    // no orphan stop_times (every trip_id kept)
    for (const st of tbl(files, "stop_times.txt").rows) expect(tripIds.has(st.trip_id)).toBe(true);

    // stops = referenced (ST1,ST2,ST3,ST4) + parent STA; ST5 (only R3) dropped
    const stopIds = new Set(tbl(files, "stops.txt").rows.map((r) => r.stop_id));
    expect([...stopIds].sort()).toEqual(["ST1", "ST2", "ST3", "ST4", "STA"]);
    expect(stopIds.has("ST5")).toBe(false);
    expect(stopIds.has("STA")).toBe(true); // parent_station pulled in

    // calendar keeps only surviving services (WD, WE); shapes keep S1,S2
    const svc = new Set(tbl(files, "calendar.txt").rows.map((r) => r.service_id));
    expect([...svc].sort()).toEqual(["WD", "WE"]);
    const shapeIds = new Set(tbl(files, "shapes.txt").rows.map((r) => r.shape_id));
    expect([...shapeIds].sort()).toEqual(["S1", "S2"]);

    // translations pruned to kept stops (ST1 kept, ST3 kept -> both stay)
    const trStops = tbl(files, "translations.txt").rows.map((r) => r.record_id).sort();
    expect(trStops).toEqual(["ST1", "ST3"]);
  });

  it("prunes translations for dropped stops", () => {
    const { files } = segmentFeed(fixture(), { routesPerAgency: 1 }); // only R1
    const trStops = tbl(files, "translations.txt").rows.map((r) => r.record_id);
    expect(trStops).toEqual(["ST1"]); // ST3 belonged to R2, now gone
  });

  it("honours explicit route_ids", () => {
    const { files } = segmentFeed(fixture(), { routesPerAgency: 1, routeIds: ["R3"] });
    expect(tbl(files, "routes.txt").rows.map((r) => r.route_id)).toEqual(["R3"]);
  });

  it("stamps the watermark feed_version", () => {
    const { files } = segmentFeed(fixture(), { routesPerAgency: 1, feedVersion: "wm-123" });
    expect(tbl(files, "feed_info.txt").rows[0].feed_version).toBe("wm-123");
  });

  it("segmentZip round-trips through a real zip", () => {
    const zip = zipSync(fixture());
    const { zip: seg, stats } = segmentZip(zip, { routesPerAgency: 2 });
    const files = unzipSync(seg);
    expect(Object.keys(files).sort()).toContain("stop_times.txt");
    expect(stats.routes).toBe(2);
  });
});
