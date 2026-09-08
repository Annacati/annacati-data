#!/usr/bin/env python3
"""bake_curated — materialize Annacati's curation + Lua edits into "gold" GTFS.

Run on demand. For each self-hosted agency it: fetches the raw R2 feed (which
already carries curated coords + pfaedle shapes), runs the EXACT composed
per-operator Lua the MOTIS box runs (via motis-setup/build_scripts.py, so no
drift) to bake in curated names/colours/headsigns/renames/translations, adds a
feed_info.txt (+ translations.txt), and writes curated/<slug>.zip plus a
curated/index.json catalogue. The Worker (data.annacati.com) then serves and
segments these on demand.

Sources (self-hosted vs external) come from the agency-registry checkout; only
self-hosted feeds (static: on feeds.annacati.com) are baked — external
third-party feeds are skipped for resale-rights cleanliness.

Usage:
  bake_curated.py --all [--env prod|beta]
  bake_curated.py --only etna-trasporti,interbus
  bake_curated.py --only etna-trasporti --local-feeds   # dev: read gtfs-collector/data/gtfs
  bake_curated.py ...   --out-dir ./out                 # default: ./curated
  bake_curated.py ...   --live-curation                 # pull canonical from curation.annacati.com

R2 upload is out of scope here (needs creds); the tool writes curated/<slug>.zip
locally and you push the directory to the annacati-data R2 bucket (e.g. with
`wrangler r2 object put`). --local-feeds lets you build+test with no token.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import transform

# ── config (env-overridable, mirrors annacati-pipeline/config.py defaults) ────
HOME = Path(os.path.expanduser("~"))
MOTIS_SETUP_DIR = Path(os.environ.get("MOTIS_SETUP_DIR", HOME / "git" / "motis-setup"))
AGENCY_REGISTRY_DIR = Path(os.environ.get("AGENCY_REGISTRY_DIR", HOME / "git" / "agency-registry"))
GTFS_COLLECTOR_DIR = Path(os.environ.get("GTFS_COLLECTOR_DIR", HOME / "git" / "gtfs-collector"))
LUA_BIN = os.environ.get("LUA_BIN", "lua")
GTFS_COLLECTOR_API_TOKEN = os.environ.get("GTFS_COLLECTOR_API_TOKEN", "")
CURATION_URL = "https://curation.annacati.com/canonical_stops.json"
USER_AGENT = os.environ.get("USER_AGENT", "annacati-data-baker/1.0")

FEEDS_BASE = {
    "prod": "https://feeds.annacati.com/gtfs",
    "beta": "https://feeds.annacati.com/beta/gtfs",
}

# GTFS tables we rewrite; everything else in the zip is copied through verbatim.
_TRANSFORMED = ("agency.txt", "stops.txt", "routes.txt", "trips.txt")


# ── registry enumeration ──────────────────────────────────────────────────────

def load_registry() -> list[dict[str, Any]]:
    """[{slug, name, static, basename, hosted}] from the local agency-registry
    checkout (generate.collect_entries — pure stdlib, no network)."""
    root = str(AGENCY_REGISTRY_DIR)
    if not AGENCY_REGISTRY_DIR.exists():
        raise SystemExit(f"agency-registry checkout not found at {root}")
    if root not in sys.path:
        sys.path.insert(0, root)
    import generate  # noqa
    entries, _warnings = generate.collect_entries()
    out = []
    for e in entries:
        static = e.get("static", "") or ""
        basename = static.rsplit("/", 1)[-1]
        if basename.lower().endswith(".zip"):
            basename = basename[:-4]
        out.append({
            "slug": e["slug"],
            "name": e.get("name") or e["slug"],
            "static": static,
            "basename": basename,
            "hosted": "r2" if static.startswith("https://feeds.annacati.com/") else "external",
        })
    return out


# ── Lua composition (the real box logic) ──────────────────────────────────────

def compose_scripts(out_dir: Path, live_curation: bool) -> None:
    """Run motis-setup/build_scripts.py to compose every per-operator <slug>.lua
    (the fast-path hand script + _lib.lua + titlecase + canonical + descname
    layers) exactly as the box does. Writes into out_dir."""
    canonical = MOTIS_SETUP_DIR / "canonical_stops.json"
    canonical_arg = CURATION_URL if live_curation else str(canonical)
    cmd = [
        sys.executable, "build_scripts.py", "scripts",
        canonical_arg,
        "titlecase_stops.json", "descname_stops.json",
        str(out_dir),
    ]
    subprocess.run(cmd, cwd=str(MOTIS_SETUP_DIR), check=True, capture_output=True, text=True)


# ── feed I/O ──────────────────────────────────────────────────────────────────

def fetch_feed(entry: dict[str, Any], env: str, local: bool, cache: Path) -> Path:
    """Return a path to the raw feed zip. --local-feeds reads the collector's
    committed data/gtfs/<basename>.zip; otherwise download from R2 (needs token)."""
    if local:
        p = GTFS_COLLECTOR_DIR / "data" / "gtfs" / f"{entry['basename']}.zip"
        if not p.exists():
            raise FileNotFoundError(f"local feed not found: {p}")
        return p
    if not GTFS_COLLECTOR_API_TOKEN:
        raise SystemExit("GTFS_COLLECTOR_API_TOKEN not set (needed to download from R2); "
                         "use --local-feeds for a dev build")
    import httpx
    url = f"{FEEDS_BASE[env]}/{entry['basename']}.zip"
    dest = cache / f"{entry['basename']}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT, "Authorization": f"Bearer {GTFS_COLLECTOR_API_TOKEN}"}
    with httpx.Client(timeout=120, follow_redirects=True) as c:
        with c.stream("GET", url, headers=headers) as r:
            r.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in r.iter_bytes(1 << 16):
                    fh.write(chunk)
    return dest


def read_table(raw: bytes) -> tuple[list[dict[str, str]], list[str]]:
    """Parse a GTFS .txt (utf-8-sig, DictReader), returning (rows, fieldnames)."""
    text = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig", newline="")
    reader = csv.DictReader(text)
    rows = list(reader)
    return rows, list(reader.fieldnames or [])


def write_table(rows: list[dict[str, str]], fieldnames: list[str]) -> bytes:
    """Serialize rows to GTFS .txt bytes. fieldnames is the original header order;
    any key present in rows but absent from it (a column the transform added, e.g.
    route_color on a feed that shipped none) is appended so nothing is dropped."""
    extra = []
    for row in rows:
        for k in row:
            if k not in fieldnames and k not in extra:
                extra.append(k)
    cols = fieldnames + extra
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\r\n")
    w.writeheader()
    for row in rows:
        w.writerow({k: row.get(k, "") for k in cols})
    return buf.getvalue().encode("utf-8")


def service_span(entries: dict[str, bytes]) -> tuple[str, str]:
    """(start, end) YYYYMMDD across calendar.txt + calendar_dates.txt, or ('','')."""
    dates: list[str] = []
    if "calendar.txt" in entries:
        rows, _ = read_table(entries["calendar.txt"])
        for r in rows:
            for k in ("start_date", "end_date"):
                v = (r.get(k) or "").strip()
                if v.isdigit() and len(v) == 8:
                    dates.append(v)
    if "calendar_dates.txt" in entries:
        rows, _ = read_table(entries["calendar_dates.txt"])
        for r in rows:
            v = (r.get("date") or "").strip()
            if v.isdigit() and len(v) == 8:
                dates.append(v)
    if not dates:
        return "", ""
    return min(dates), max(dates)


def feed_info_txt(publisher_url: str, lang: str, start: str, end: str, version: str) -> bytes:
    row = {
        "feed_publisher_name": "Annacati",
        "feed_publisher_url": publisher_url,
        "feed_lang": lang,
        "feed_version": version,
    }
    cols = ["feed_publisher_name", "feed_publisher_url", "feed_lang", "feed_version"]
    if start and end:
        row["feed_start_date"], row["feed_end_date"] = start, end
        cols += ["feed_start_date", "feed_end_date"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\r\n")
    w.writeheader()
    w.writerow(row)
    return buf.getvalue().encode("utf-8")


# ── bake one agency ───────────────────────────────────────────────────────────

def bake_one(
    entry: dict[str, Any], env: str, local: bool, scripts_dir: Path,
    cache: Path, out_dir: Path,
) -> dict[str, Any]:
    slug = entry["slug"]
    feed_path = fetch_feed(entry, env, local, cache)

    # Read the whole zip into memory; copy everything through, rewrite only the
    # transformed tables (+ feed_info/translations). stop_times/shapes/calendar
    # are large and untouched, so they pass through verbatim.
    entries: dict[str, bytes] = {}
    with zipfile.ZipFile(feed_path) as zf:
        for n in zf.namelist():
            if not n.endswith("/"):
                entries[n.split("/")[-1]] = zf.read(n)

    tables: dict[str, list[dict[str, str]]] = {}
    fieldnames: dict[str, list[str]] = {}
    for name in _TRANSFORMED:
        if name in entries:
            tables[name], fieldnames[name] = read_table(entries[name])
        else:
            tables[name], fieldnames[name] = [], []

    # Compose is already done for all slugs into scripts_dir; a feed with no
    # composed script (no hand script, no canonical/titlecase/descname entry) is
    # an identity transform — nothing to apply.
    composed_path = scripts_dir / f"{slug}.lua"
    summary = {k: 0 for k in (
        "routes_changed", "stops_renamed", "stops_repositioned",
        "headsigns_changed", "agency_renamed", "translations")}
    if composed_path.exists():
        composed = composed_path.read_text(encoding="utf-8")
        mut = transform.compute_transforms(tables, composed, lua_bin=LUA_BIN)
        if "error" in mut or "load_error" in mut:
            return {"slug": slug, "error": mut.get("error") or mut.get("load_error"),
                    "detail": mut.get("detail")}
        summary = transform.apply_transforms(tables, mut)

    # Re-serialize the transformed tables back into the zip entry set.
    for name in _TRANSFORMED:
        if name in entries or tables[name]:
            entries[name] = write_table(tables[name], fieldnames[name])
    if "translations.txt" in tables:  # added by apply_transforms
        entries["translations.txt"] = write_table(
            tables["translations.txt"],
            ["table_name", "field_name", "language", "translation", "record_id"])

    start, end = service_span(entries)
    version = datetime.now(timezone.utc).strftime("%Y%m%d")
    entries["feed_info.txt"] = feed_info_txt(
        "https://annacati.com", "it", start, end, f"annacati-{slug}-{version}")

    # Write the curated zip deterministically (sorted names) so re-bakes are
    # comparable, and hash it for the catalogue.
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.zip"
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(entries):
            zf.writestr(name, entries[name])
    data = blob.getvalue()
    out_path.write_bytes(data)

    n_routes = len(tables["routes.txt"])
    n_trips = len(read_table(entries["trips.txt"])[0]) if "trips.txt" in entries else 0
    n_stops = len(tables["stops.txt"])
    return {
        "slug": slug, "name": entry["name"], "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "service_start": start, "service_end": end,
        "routes": n_routes, "trips": n_trips, "stops": n_stops,
        "transforms": summary,
        "baked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bake curated GTFS feeds")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="bake every self-hosted agency")
    g.add_argument("--only", help="comma-separated agency slugs")
    ap.add_argument("--env", choices=["prod", "beta"], default="prod")
    ap.add_argument("--local-feeds", action="store_true",
                    help="read gtfs-collector/data/gtfs instead of R2 (dev)")
    ap.add_argument("--live-curation", action="store_true",
                    help="pull canonical_stops.json from curation.annacati.com")
    ap.add_argument("--out-dir", default="curated")
    args = ap.parse_args(argv)

    registry = load_registry()
    by_slug = {e["slug"]: e for e in registry}

    if args.all:
        wanted = [e for e in registry if e["hosted"] == "r2"]
    else:
        slugs = [s.strip() for s in args.only.split(",") if s.strip()]
        wanted = []
        for s in slugs:
            if s not in by_slug:
                print(f"[skip] {s}: not in registry", file=sys.stderr)
                continue
            wanted.append(by_slug[s])

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    with tempfile.TemporaryDirectory(prefix="bake-scripts-") as td_scripts, \
         tempfile.TemporaryDirectory(prefix="bake-cache-") as td_cache:
        scripts_dir = Path(td_scripts)
        compose_scripts(scripts_dir, args.live_curation)
        cache = Path(td_cache)

        for entry in wanted:
            if entry["hosted"] != "r2":
                skipped.append({"slug": entry["slug"], "reason": "external feed (not self-hosted)"})
                print(f"[skip] {entry['slug']}: external feed", file=sys.stderr)
                continue
            try:
                res = bake_one(entry, args.env, args.local_feeds, scripts_dir, cache, out_dir)
            except Exception as e:  # noqa: BLE001
                skipped.append({"slug": entry["slug"], "reason": str(e)})
                print(f"[error] {entry['slug']}: {e}", file=sys.stderr)
                continue
            if "error" in res:
                skipped.append({"slug": entry["slug"], "reason": res["error"]})
                print(f"[error] {entry['slug']}: {res['error']}", file=sys.stderr)
                continue
            results.append(res)
            t = res["transforms"]
            print(f"[ok] {entry['slug']}: {res['routes']} routes, {res['stops']} stops, "
                  f"{res['trips']} trips | +{t['routes_changed']} colours, "
                  f"{t['stops_renamed']} renamed, {t['agency_renamed']} agency, "
                  f"{t['translations']} translations", file=sys.stderr)

    index = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "env": args.env,
        "feeds": sorted(results, key=lambda r: r["slug"]),
        "skipped": skipped,
    }
    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nBaked {len(results)} feed(s), skipped {len(skipped)}. Index: {out_dir/'index.json'}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
