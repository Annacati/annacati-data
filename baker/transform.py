"""Materialize Annacati's curation + Lua edits into GTFS feed bytes.

The value we sell over a raw scrape is our edits, and they do NOT live in the
feed bytes: MOTIS applies them in-memory at import time. The two layers are:

  * curation — canonical stop names, repositioned coords, translations, and
    same-name hub unification (from canonical_stops.json), plus the title-case
    and stop-name<-description normalizations.
  * per-operator Lua — route brand colours (+ WCAG contrast), route short-name
    tidy, coach route_type, trip-headsign rewrites, agency renames.

Both are expressed as the composed per-operator Lua script that the MOTIS box
runs. This module runs that EXACT composed script (produced by
`motis-setup/build_scripts.py`, see bake_curated.compose_scripts) over a feed's
tables through a faithful model of the MOTIS import object API, and writes the
mutations back — the same object-model shim `annacati-pipeline`'s `lua_preview`
tool uses to PREVIEW an edit, but here it emits the final field values so we can
re-serialize the feed instead of a diff. Running the real composed Lua (not a
re-implementation) is what keeps this from drifting from what MOTIS routes on.

What is NOT materialized: hub *merges*. MOTIS auto-links same-named stops within
~300 m at routing time; that is a routing behaviour, not feed bytes. The curated
feed carries the unified names + coords (exactly what MOTIS sees), which is the
faithful representation. (A future pass could emit transfers.txt to encode it.)

The small pure helpers (parse_color/color_hex/lua_literal/_lua_str) mirror
annacati-pipeline/pipeline_mcp/tools/lua_preview.py byte-for-byte; they are
vendored here so this repo does not hard-depend on the pipeline package layout.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_LUA_TIMEOUT = 60  # a whole feed's transform over the shim is well under this.


# ── colour (pure; mirrors lua_preview) ───────────────────────────────────────

def parse_color(raw: str | None) -> int:
    """A GTFS route_color hex ("196E81", no '#') to the int get_color() returns.
    Empty/blank/malformed -> 0 (MOTIS's "unset" sentinel; scripts test == 0)."""
    s = (raw or "").strip().lstrip("#")
    if len(s) != 6:
        return 0
    try:
        return int(s, 16)
    except ValueError:
        return 0


def color_hex6(value: int) -> str:
    """An int colour to GTFS's 6-hex-upper (no '#'), or "" for the 0 sentinel."""
    if not value:
        return ""
    return f"{value & 0xFFFFFF:06X}"


# ── Lua literal emission (pure; mirrors lua_preview) ──────────────────────────

def _lua_str(s: str) -> str:
    out = (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    out = "".join(c if ord(c) >= 0x20 or c == "\\" else f"\\{ord(c)}" for c in out)
    return '"' + out + '"'


def lua_literal(value: Any) -> str:
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _lua_str(value)
    if isinstance(value, list):
        return "{" + ",".join(lua_literal(v) for v in value) + "}"
    if isinstance(value, dict):
        parts = [f"[{_lua_str(str(k))}]={lua_literal(v)}" for k, v in value.items()]
        return "{" + ",".join(parts) + "}"
    raise TypeError(f"cannot serialize {type(value)!r} to Lua")


def _int_or(raw: str | None, default: int) -> int:
    try:
        return int((raw or "").strip())
    except ValueError:
        return default


def _float_or(raw: str | None, default: float) -> float:
    try:
        return float((raw or "").strip())
    except ValueError:
        return default


# ── feed tables -> Lua data (pure) ────────────────────────────────────────────

def build_data(tables: dict[str, list[dict[str, str]]]) -> dict[str, Any]:
    """Reduce the feed to the fields the modeled object API exposes.

    Routes, stops and agencies pass through (each row is distinct by id). Trips
    are deduped by headsign: in the MOTIS object model a trip exposes ONLY its
    headsign, so process_trip is a pure function of the headsign — computing it
    once per distinct headsign and mapping every trip back by its original
    headsign is exact and collapses a million-row trips.txt to its headsigns.
    """
    routes = []
    for r in tables.get("routes.txt", []):
        routes.append({
            "route_id": r.get("route_id", ""),
            "long_name": r.get("route_long_name", ""),
            "short_name": r.get("route_short_name", ""),
            "color": parse_color(r.get("route_color")),
            "text_color": parse_color(r.get("route_text_color")),
            "route_type": _int_or(r.get("route_type"), 0),
        })

    stops = []
    for s in tables.get("stops.txt", []):
        stops.append({
            "id": s.get("stop_id", ""),
            "name": s.get("stop_name", ""),
            "description": s.get("stop_desc", ""),
            "lat": _float_or(s.get("stop_lat"), 0.0),
            "lng": _float_or(s.get("stop_lon"), 0.0),
        })

    agencies = []
    for a in tables.get("agency.txt", []):
        agencies.append({"id": a.get("agency_id", ""), "name": a.get("agency_name", "")})

    headsigns = sorted({t.get("trip_headsign", "") for t in tables.get("trips.txt", [])})
    trips = [{"headsign": h} for h in headsigns]

    return {"routes": routes, "stops": stops, "agencies": agencies, "trips": trips}


# ── the write-back driver ─────────────────────────────────────────────────────
# Same object-model shim as lua_preview's _DRIVER, but the run loop emits each
# entity's FINAL field values keyed by id (plus a headsign map) so the caller can
# re-serialize the feed, and Stop captures the full translated set for
# translations.txt.

_DRIVER = r"""
COACH_SERVICE = 200

translation = {}
function translation.new(locale, text)
  return { __translation = true, locale = locale, text = text }
end

-- set_name may receive a plain string OR an ordered table of translation.new().
-- Return (default_italian_text, ordered_pairs|nil).
local function name_of(v)
  if type(v) == "string" then return v, nil end
  if type(v) == "table" then
    local pairs_out = {}
    local default = nil
    for _, t in ipairs(v) do
      if type(t) == "table" and t.__translation then
        pairs_out[#pairs_out+1] = { t.locale, t.text }
        if t.locale == "it" and default == nil then default = t.text end
      end
    end
    if default == nil and pairs_out[1] then default = pairs_out[1][2] end
    return default or "", pairs_out
  end
  return tostring(v), nil
end

local Route = {}; Route.__index = Route
function Route.new(r) return setmetatable({_c=r.color,_tc=r.text_color,_sn=r.short_name,_rt=r.route_type}, Route) end
function Route:get_color() return self._c end
function Route:set_color(v) self._c = v end
function Route:get_text_color() return self._tc end
function Route:set_text_color(v) self._tc = v end
function Route:get_short_name() return self._sn end
function Route:set_short_name(v) self._sn = v end
function Route:get_route_type() return self._rt end
function Route:set_route_type(v) self._rt = v end

local Pos = {}; Pos.__index = Pos
function Pos.new(lat, lng) return setmetatable({_lat=lat,_lng=lng}, Pos) end
function Pos:get_lat() return self._lat end
function Pos:get_lng() return self._lng end
function Pos:set_lat(v) self._lat = v end
function Pos:set_lng(v) self._lng = v end

local Stop = {}; Stop.__index = Stop
function Stop.new(s) return setmetatable({_id=s.id,_name=s.name,_desc=s.description,_lat=s.lat,_lng=s.lng,_names=nil}, Stop) end
function Stop:get_id() return self._id end
function Stop:get_name() return self._name end
function Stop:set_name(v) local d, pairs_out = name_of(v); self._name = d; self._names = pairs_out end
function Stop:get_description() return self._desc end
function Stop:get_pos() return Pos.new(self._lat, self._lng) end
function Stop:set_pos(p) self._lat = p:get_lat(); self._lng = p:get_lng() end

local Trip = {}; Trip.__index = Trip
function Trip.new(t) return setmetatable({_hs=t.headsign}, Trip) end
function Trip:get_headsign() return self._hs end
function Trip:set_headsign(v) self._hs = v end

local Agency = {}; Agency.__index = Agency
function Agency.new(a) return setmetatable({_name=a.name}, Agency) end
function Agency:get_name() return self._name end
function Agency:set_name(v) self._name = v end

-- ==== minimal JSON encoder ====
local function enc(v)
  local t = type(v)
  if v == nil then return "null" end
  if t == "boolean" then return v and "true" or "false" end
  if t == "number" then
    if v ~= v or v == math.huge or v == -math.huge then return "null" end
    return string.format("%.14g", v)
  end
  if t == "string" then
    local s = v:gsub('[%z\1-\31\\"]', function(c)
      local m = {['"']='\\"', ['\\']='\\\\', ['\n']='\\n', ['\r']='\\r', ['\t']='\\t'}
      return m[c] or string.format('\\u%04x', string.byte(c))
    end)
    return '"' .. s .. '"'
  end
  if t == "table" then
    local n = 0
    for _ in pairs(v) do n = n + 1 end
    if #v == n then
      local parts = {}
      for i = 1, #v do parts[i] = enc(v[i]) end
      return "[" .. table.concat(parts, ",") .. "]"
    end
    local parts = {}
    for k, val in pairs(v) do parts[#parts+1] = enc(tostring(k)) .. ":" .. enc(val) end
    return "{" .. table.concat(parts, ",") .. "}"
  end
  return "null"
end

local DATA = __DATA__

local chunk, load_err = load(__SOURCE__, "@composed.lua")
if not chunk then io.write(enc({ load_error = load_err })); return end
local ok_load, run_err = pcall(chunk)
if not ok_load then io.write(enc({ load_error = run_err })); return end

local out = { routes = {}, stops = {}, headsigns = {}, agencies = {}, errors = {} }

if type(process_route) == "function" then
  for _, r in ipairs(DATA.routes) do
    local o = Route.new(r)
    local ok, err = pcall(process_route, o)
    if not ok then out.errors[#out.errors+1] = { entry="process_route", id=r.route_id, msg=err }; break end
    out.routes[r.route_id] = { color=o._c, text_color=o._tc, short_name=o._sn, route_type=o._rt }
  end
end

if type(process_location) == "function" then
  for _, s in ipairs(DATA.stops) do
    local o = Stop.new(s)
    local ok, err = pcall(process_location, o)
    if not ok then out.errors[#out.errors+1] = { entry="process_location", id=s.id, msg=err }; break end
    local rec = { name=o._name, description=o._desc, lat=o._lat, lng=o._lng }
    if o._names and #o._names > 0 then rec.names = o._names end
    out.stops[s.id] = rec
  end
end

if type(process_trip) == "function" then
  for _, t in ipairs(DATA.trips) do
    local o = Trip.new(t)
    local ok, err = pcall(process_trip, o)
    if not ok then out.errors[#out.errors+1] = { entry="process_trip", id=t.headsign, msg=err }; break end
    if o._hs ~= t.headsign then out.headsigns[t.headsign] = o._hs end
  end
end

if type(process_agency) == "function" then
  for _, a in ipairs(DATA.agencies) do
    local o = Agency.new(a)
    local ok, err = pcall(process_agency, o)
    if not ok then out.errors[#out.errors+1] = { entry="process_agency", id=a.id, msg=err }; break end
    out.agencies[a.id] = { name=o._name }
  end
end

io.write(enc(out))
"""


def build_driver(source: str, data: dict[str, Any]) -> str:
    return _DRIVER.replace("__DATA__", lua_literal(data)).replace("__SOURCE__", _lua_str(source))


def run_lua(driver: str, lua_bin: str = "lua") -> dict[str, Any]:
    """Run the driver, returning parsed JSON or an {error: ...} dict."""
    exe = shutil.which(lua_bin)
    if not exe:
        return {"error": f"lua interpreter not found (looked for {lua_bin!r} on PATH)"}
    with tempfile.TemporaryDirectory(prefix="gtfs-bake-") as td:
        drv = Path(td) / "driver.lua"
        drv.write_text(driver, encoding="utf-8")
        try:
            proc = subprocess.run(
                [exe, str(drv)], capture_output=True, text=True, timeout=_LUA_TIMEOUT, cwd=td,
            )
        except subprocess.TimeoutExpired:
            return {"error": f"lua run exceeded {_LUA_TIMEOUT}s"}
    if proc.returncode != 0:
        return {"error": "lua run failed", "detail": (proc.stderr or proc.stdout).strip()}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": "lua produced non-JSON output", "detail": proc.stdout[:2000]}


def compute_transforms(
    tables: dict[str, list[dict[str, str]]],
    composed_lua: str,
    lua_bin: str = "lua",
) -> dict[str, Any]:
    """Run the composed Lua over the feed and return the mutations to apply:
    {routes: {route_id: {...}}, stops: {stop_id: {...}}, headsigns: {old: new},
    agencies: {agency_id: {name}}}. On failure returns {error/load_error/...}."""
    data = build_data(tables)
    raw = run_lua(build_driver(composed_lua, data), lua_bin=lua_bin)
    if "error" in raw or "load_error" in raw:
        return raw
    if raw.get("errors"):
        return {"error": "composed Lua raised", "detail": raw["errors"]}
    # The driver's Lua JSON encoder emits an EMPTY table as [] (it can't tell an
    # empty map from an empty array). Coerce the id-keyed maps back to dicts so
    # apply_transforms can .get() them.
    for key in ("routes", "stops", "headsigns", "agencies"):
        if isinstance(raw.get(key), list):
            raw[key] = {}
    return raw


# ── apply mutations back into the tables (pure) ───────────────────────────────

def apply_transforms(
    tables: dict[str, list[dict[str, str]]],
    mutations: dict[str, Any],
) -> dict[str, int]:
    """Write the computed mutations back into the row-dict tables IN PLACE.
    Returns a summary count dict."""
    summary = {
        "routes_changed": 0, "stops_renamed": 0, "stops_repositioned": 0,
        "headsigns_changed": 0, "agency_renamed": 0, "translations": 0,
    }

    routes = mutations.get("routes", {})
    for r in tables.get("routes.txt", []):
        m = routes.get(r.get("route_id", ""))
        if not m:
            continue
        changed = False
        new_color = color_hex6(m["color"])
        new_text = color_hex6(m["text_color"])
        # Only overwrite a colour the script actually set (non-empty); leave the
        # feed's own colour otherwise so we never blank a shipped colour.
        if new_color and new_color != (r.get("route_color") or "").upper():
            r["route_color"] = new_color
            changed = True
        if new_text and new_text != (r.get("route_text_color") or "").upper():
            r["route_text_color"] = new_text
            changed = True
        if m["short_name"] != r.get("route_short_name", ""):
            r["route_short_name"] = m["short_name"]
            changed = True
        rt = str(m["route_type"])
        if rt != (r.get("route_type") or ""):
            r["route_type"] = rt
            changed = True
        if changed:
            summary["routes_changed"] += 1

    stops = mutations.get("stops", {})
    translations: list[dict[str, str]] = []
    for s in tables.get("stops.txt", []):
        m = stops.get(s.get("stop_id", ""))
        if not m:
            continue
        if m["name"] and m["name"] != s.get("stop_name", ""):
            s["stop_name"] = m["name"]
            summary["stops_renamed"] += 1
        # Coordinates: keep the feed's string form unless the value differs.
        new_lat, new_lng = _fmt_coord(m["lat"]), _fmt_coord(m["lng"])
        if (new_lat != (s.get("stop_lat") or "") or new_lng != (s.get("stop_lon") or "")):
            s["stop_lat"], s["stop_lon"] = new_lat, new_lng
            summary["stops_repositioned"] += 1
        for loc, text in m.get("names", []):
            if loc == "it":
                continue
            translations.append({
                "table_name": "stops", "field_name": "stop_name",
                "language": loc, "translation": text,
                "record_id": s.get("stop_id", ""),
            })
    if translations:
        tables["translations.txt"] = translations
        summary["translations"] = len(translations)

    headsigns = mutations.get("headsigns", {})
    if headsigns:
        for t in tables.get("trips.txt", []):
            new = headsigns.get(t.get("trip_headsign", ""))
            if new is not None and new != t.get("trip_headsign", ""):
                t["trip_headsign"] = new
                summary["headsigns_changed"] += 1

    agencies = mutations.get("agencies", {})
    for a in tables.get("agency.txt", []):
        m = agencies.get(a.get("agency_id", ""))
        if m and m["name"] and m["name"] != a.get("agency_name", ""):
            a["agency_name"] = m["name"]
            summary["agency_renamed"] += 1

    return summary


def _fmt_coord(v: float) -> str:
    """Coord round mirrors the interop contract: round(x, 6). Trailing-zero and
    integer forms match round()'s repr so an unchanged coord re-serializes
    identically for the common case."""
    r = round(float(v), 6)
    if r == int(r):
        return str(int(r))
    return repr(r)
