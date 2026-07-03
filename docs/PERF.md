# Performance plan — multi-commune (metropolitan) imports

*Draft 2026-07-03. Numbers below are measured on this machine against the real
`~/.sirene/StockEtablissement.parquet`, not estimated.*

## 1. Which uses of the tool this targets

| Tool | Pattern | Targeted? |
|---|---|---|
| **`fdp_import_lot`** — "Import en lot" | N new communes × (WFS layers + SIRENE + analysis) in one run | **Primary.** This is the 20-commune metro case. |
| **`fdp_ajout_couches`** — "Ajout aux communes existantes" | Loops existing communes, loads the *selected* new layers (a subset) + optionally SIRENE | **Secondary.** Same per-commune costs, usually fewer layers. |
| `fdp_par_commune` — single import | 1 commune × (WFS + SIRENE) | **Not a target, must not regress.** ~15 fetches + one ~1.8 s SIRENE scan is fine. Optimizations must leave this path unchanged / no slower. |
| `axono_batiments`, `fdp_bati_dans_zone`, `fdp_pavillonnaire`, `fdp_gestionnaire_themes` | Post-process already-loaded layers | **Unaffected** — no WFS/SIRENE. |

Design rule: the wins below apply to the **batch** path. The single-commune path
stays byte-for-byte as-is (it's already acceptable and low-risk).

## 2. Where the time goes (measured / code-verified)

**WFS layer loads — #1, dominant.** `_run_commune_import` loops `_load_wfs_layer`
serially; each call builds a live `QgsVectorLayer(uri,"WFS")` (the `isValid()` /
`featureCount()` calls force a blocking network fetch) **plus** a `native:clip`
(CPU). A metro of 20 communes × ~10–15 layers = **~200–300 serial fetch+clip
round-trips on the UI thread.**

**SIRENE parquet scans — #2, cleanly fixable.** Measured, warm cache:

| | current | with prefetch |
|---|---|---|
| stock only (your legacy merged file) | 20 × 1.8 s ≈ **37 s** | one batched read ≈ **1.9 s** |
| two-file path (fresh installs) | 40 × 1.8 s ≈ **74 s** | two batched reads ≈ **3.8 s** |

*(First cold read was 4.2 s; warm steady-state is 1.8 s. Per-commune legacy
schema check is 37 ms — negligible.)* The scan cost is fixed per read regardless
of how many communes are in the filter, so `codeCommune IN [all N]` once ≈ the
cost of a single commune.

**Per-commune building analysis (#3)** — spatial joins scaling with each commune's
buildings; already `QgsSpatialIndex`-backed; essentially irreducible.

**geo API lookups (#4)** — one `requests.get` per commune; negligible.

## 3. Options — with the caveats that checking the code surfaced

### A. SIRENE prefetch — recommended, low risk
Read the parquet(s) **once** for the whole batch (`codeCommune IN [all N]`),
partition by commune in memory, serve each commune from that. Additive: a
`_prefetch_sirene(codes)` populates an instance cache that `_load_sirene` consults
before hitting disk; single-commune runs never populate it, so that path is
unchanged. Saves ~35 s (your case) to ~70 s (two-file). **Provable offline** against
your file before you ever run QGIS.
- Caveat: PLM communes (Paris/Lyon/Marseille as a whole → arrondissement codes)
  expand the code set; prefetch will simply skip PLM parents and let them fall
  through to the existing per-commune path. Metros use arrondissements directly, so
  this is a non-issue in practice.

### B. WFS — the big lever, but geography matters (this is the "checking my work" catch)
Two routes, and the naïve one is **not universally safe**:

- **B1. One fetch per layer over the whole-batch bbox, then clip per commune** —
  ~15 fetches instead of 300. **But it's only valid when the communes are
  geographically contiguous** (a real metro). If someone pastes 20 scattered
  communes, the union bbox becomes most of France and the bâti query explodes /
  gets truncated by the WFS `maxFeatures` cap. So B1 needs (i) a contiguity/ös
  bbox-area guard that falls back to per-commune when the spread is too large, and
  (ii) **paging** (`STARTINDEX`/`COUNT`) because a dense metro's bâti exceeds the
  server page limit. Medium effort, geography-conditional.

- **B2. Parallelize the fetches** — WFS fetches are independent and network-bound,
  so overlapping them is the general-purpose win (works for any geography). Safest
  concrete form: fetch each layer's GeoJSON with `requests` in a bounded
  `ThreadPoolExecutor` (network only, off the main thread), then build the
  `QgsVectorLayer`s and run `native:clip` **on the main thread** (QGIS objects are
  not thread-safe). This keeps QGIS object creation single-threaded while
  overlapping the network waits. More code than B1, but no geography assumption.
  Note: clips stay serial/CPU-bound either way; parallelism helps the network wait,
  which is the bulk.

## 4. Recommended phasing

1. **SIRENE prefetch (A)** — do first. Contained, measurable (~35–70 s), verifiable
   offline, single-commune path untouched. Applies to `fdp_import_lot` and
   `fdp_ajout_couches`.
2. **WFS (B)** — decide B1 vs B2 after seeing which the log actually lingers on.
   Lean **B2** for correctness across arbitrary commune lists; consider **B1** only
   if metros are the sole batch use and we add the contiguity guard + paging.

## 5. How each win will be verified
- **A:** offline — batched read of your real file returns the same per-commune rows
  as N single reads (row-for-row), and times ~1.9 s vs ~37 s. Then a QGIS metro
  import to confirm identical SIRENE output.
- **B:** a small metro (3–5 communes) import, diffing the resulting per-commune
  layer feature counts against the current serial path (must match), then timing.
