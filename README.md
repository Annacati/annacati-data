# annacati-data

Sellable **curated GTFS** extracts — sample bundles to email prospects now, a
paid data API later. Lives at `data.annacati.com`.

Canonical docs are in the wiki: `services/annacati-data/README.md`. This file is
a thin pointer (project convention: notes live in the wiki, not the source repo).

## Two parts

- **`baker/`** (Python, run on demand) — materializes our curation + Lua edits
  into "gold" GTFS feeds and writes `curated/<slug>.zip` + `curated/index.json`.
  The transform can't run in a Worker (needs `build_scripts.py` + a `lua`
  interpreter), so it's baked offline into the `annacati-data` R2 bucket.
  ```
  python -m venv .venv && .venv/bin/pip install -r baker/requirements.txt
  .venv/bin/python baker/bake_curated.py --only etna-trasporti,interbus --local-feeds --out-dir curated
  # then push curated/ to R2, e.g.:
  #   wrangler r2 object put annacati-data/curated/index.json --file curated/index.json
  #   for f in curated/*.zip; do wrangler r2 object put annacati-data/curated/$(basename $f) --file $f; done
  ```
- **`src/`** (Worker) — the API surface. `GET /catalog`, `GET /extract`
  (`mode=sample|full`), `GET /health`. Full = R2 passthrough; sample = live
  segmentation (N complete routes/agency). Token-gated, with an entitlement seam
  and Analytics Engine metering wired for future paid tiers.
  ```
  npm install && npm run typecheck && npm test
  npm run dev   # then curl -H "Authorization: Bearer $TOKEN" localhost:8787/catalog
  ```

## Usage (the email flow)

```
curl -H "Authorization: Bearer $DATA_API_ADMIN_TOKEN" \
  "https://data.annacati.com/extract?agencies=etna-trasporti,interbus&mode=sample&routes_per_agency=3" \
  -o annacati-sample.zip
```
Then email `annacati-sample.zip`.
