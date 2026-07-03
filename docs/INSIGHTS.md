# Code Health & Modernization — FDP-par-Commune toolset

*Deep-sweep review, 2026-07-03. Findings are ordered by value-to-risk ratio.
Line numbers are approximate — treat them as "look near here", not exact anchors.*

---

## 1. How the toolset actually fits together (the mental model)

Understanding the wiring makes every finding below make sense.

**Hub-and-spoke via `importlib`.** `fdp_par_commune.py` is the hub (~3,400 lines). Because
QGIS Processing scripts are loaded without a package (`__package__` is empty), normal imports
(`from sirene_buildings import ...`) fail. So the project loads its own sibling modules by
absolute path:

```python
_spec = importlib.util.spec_from_file_location(
    "sirene_buildings",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sirene_buildings.py"))
_mod  = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_mod)
build_activity_layers = _mod.build_activity_layers
```

This 6-line ritual is repeated **5× in `fdp_par_commune.py` and 3× in `fdp_ajout_couches.py`**,
and again inside `sirene_buildings.py` and `bati_buildings.py` (to reach `zone_buildings.generate_gradient`).
It works, but it's the single most "hobbled-together" thing in the codebase: the dependency
graph is invisible to any tool, there's no import-error surface until runtime, and every new
helper adds another copy of the ritual.

The live dependency graph:

```
fdp_par_commune.py ─┬─> sirene_buildings ─> zone_buildings
                    ├─> bati_buildings   ─> zone_buildings
                    ├─> zone_buildings
                    ├─> sirene_display   ─> sirene_buildings
                    └─> theme_manager
fdp_ajout_couches / fdp_import_lot ── subclass FDPParCommune
fdp_gestionnaire_themes ── launches theme_manager
axono_batiments · fdp_bati_dans_zone · fdp_pavillonnaire ── standalone
```

**SIRENE data lineage (3 generations, only the last is live):**
1. per-department CSV.gz from `files.data.gouv.fr/geo-sirene` — gone.
2. live API `recherche-entreprises.api.gouv.fr/search` — gone ("shutdown April 2026").
3. **current:** one-time bulk download of the national INSEE stock file
   `StockEtablissement_utf8.parquet` (pyarrow, cached `~/.sirene/`, 35-day TTL), read locally
   with a pushed-down filter on `codeCommuneEtablissement`. See `_load_sirene`.
   *(The commit message calls this "CSV" — it is actually Parquet.)*

**Four independent, authoritative color palettes** (this is by design, not a bug):
`SIRENE_CATEGORIES` (sirene_buildings.py), `ZAI_CATEGORIES` + `_OUTDOOR_PUBLIC_COLORS`
(zone_buildings.py), `_BUCKET_SPECS` + density/height ramps (bati_buildings.py), and the
point-marker palette inside `_apply_sirene_style` (fdp_par_commune.py).

---

## 2. Tier 1 — Safe, high-value (recommended first pass)

### A. SIRENE color drift — a real latent bug
`SIRENE_CATEGORIES` drives the **building-fill** layers (`sirene_buildings.py:48`). A **second,
hand-maintained** palette drives the **point markers** in `_apply_sirene_style`
(`fdp_par_commune.py` ~`:2388`, the `groups = [...]` list). A comment at `sirene_buildings.py:39`
explicitly says the two "must stay synchronized" — **they've drifted on ~10 of 12 categories**:

| Category | fill (`SIRENE_CATEGORIES`) | point (`_apply_sirene_style`) |
|---|---|---|
| Commerce | `#F07030` | `#F4A261` |
| Santé & action sociale | `#00C896` | `#06D6A0` |
| Culture, sport & loisirs | `#0077C8` | `#118AB2` |
| Industrie, artisanat & constr. | `#C06828` | `#8B5E3C` |
| Transport & logistique | `#4488CC` | `#6C757D` |
| … | (diverges) | (diverges) |

The building fill and the point that sits on top of it are supposed to read as the same
category color; today they don't quite match. **Fix:** delete the second palette and have
`_apply_sirene_style` pull its colors from `SIRENE_CATEGORIES` (keep shape/size local). One
source of truth, drift becomes impossible. *(This changes visible point colors — decide
whether points should adopt the fill colors or vice-versa before implementing.)*

### B. RPG typenames hard-pinned to 2024 — will silently break
`fdp_par_commune.py:286–315`. Four of the six RPG rural layers embed the year in the WFS
typename, e.g. `IGNF_RPG_PARCELLES-AGRICOLES-CATEGORISEES_2024:...`, and one even embeds a
generation date (`...surfaces_2024_zdh_20250621`). The catalogue comment already warns these
"sont épinglées à 2024 — mettre à jour pour l'édition 2025". When IGN retires the 2024 editions
those layers return empty with no error. **Fix:** hoist a single `RPG_YEAR = "2024"` constant
and build the typenames from it; the annual update becomes a one-line change. Also refresh the
stale `# Typenames vérifiés … 2025-03-04` comment.

### C. Per-commune zone re-transform — the one real O(n·m) hotspot
`fdp_bati_dans_zone.py:148–155`. The zone-layer geometries are re-fetched and
`zone_geom.transform(xform)`-ed **inside** the per-commune loop, so with N communes every zone
polygon is transformed N times. **Fix:** build the transformed zone geometries and a single
`QgsSpatialIndex` over them **once**, before the commune loop. (The rest of the codebase already
uses spatial-index + bbox pre-filter correctly — this is the lone exception.)

### D. Duplicated commune-lookup client
`_search_commune` (`:1449`) and `_lookup_commune_batch` (`:1535`) each redefine the same
`geo.api.gouv.fr` base URL, `fields=`/`type=` query constants, and a copy-pasted 4-attempt
backoff loop (`_fetch` vs `_get`). **Fix:** one module-level `GEO_API_BASE` + one `_geo_fetch()`
helper, called by both. Removes ~30 duplicated lines and a future "fixed it in one place only" bug.

---

## 3. Tier 2 — Structural (medium effort, high maintainability payoff)

- **E. Centralize configuration.** URLs (`geo.api.gouv.fr`, `data.geopf.fr/wfs`, the SIRENE
  parquet URL), `timeout=15`, cache dir + 35-day TTL, `RPG_YEAR`, and all palettes are inline
  literals scattered across methods. A single `constants.py` (or a config block at the top of
  the hub) makes the toolset far easier to retune and audit. The `_LAYER_CATALOGUE` list is
  already a great example of the data-driven style to extend.
- **F. Data-drive the ~15 `_apply_*_style` methods.** They repeat the same
  "build `QgsRuleBasedRenderer` root → append rules → `setRenderer` → `triggerRepaint`" scaffold
  with hex colors inline (~164 hex/rule occurrences in the file). A palette dict + one generic
  `build_rule_renderer(rules_spec)` would collapse most of `:1967–2833`.
- **G. Extract the `importlib` sibling-loader** (§1) into one `_load_sibling("name", "attr", …)`
  helper, used everywhere instead of the 8 copies.
- **H. Unify duplicated color utilities.** Two different `_darken()` implementations
  (`fdp_bati_dans_zone.py:227` HSV vs `axono_batiments.py:79` RGB) and two renderer-color
  extractors (`_extract_fill_color` vs `_color_for_feature`) solve the same problems differently.
  One shared `color_utils.py`.
- **I. Halve building memory in `fdp_bati_dans_zone.py:135–139`.** It keeps three parallel dicts
  per building — a spatial index, a **geometry copy**, and the **full feature**. The feature
  already carries its geometry; drop `bld_geom`.

---

## 4. Tier 3 — Larger / future-proofing (plan deliberately)

- **J. Split the monolith.** `fdp_par_commune.py` mixes HTTP clients, GDAL/OGR conversion,
  geometry ops, ~15 styling routines, and a full PyQt dialog builder in one 3,400-line file.
  Natural modules: `geo_api.py`, `wfs_loader.py`, `sirene.py`, `styles.py`, `dialogs.py`. (Do this
  *after* G, so the split isn't fighting the importlib boilerplate.)
- **K. Qt6 / future-QGIS readiness — a migration already half-started.** The code mixes modern
  and legacy APIs: `QMetaType.Type.QString` at `:1900` but `QVariant.String` at `:2322` (same
  file); unscoped enums `Qt.SolidLine`, `Qt.Checked` (needs `Qt.PenStyle.SolidLine`,
  `Qt.CheckState.Checked`); `.exec_()` (→ `.exec()`); `QgsUnitTypes.RenderMillimeters`
  (→ `Qgis.RenderUnit.Millimeters`); `QgsSymbolLayer.PropertyFillColor`
  (→ `QgsSymbolLayer.Property.FillColor`). None break today; all break on a PyQt6/Qt6 QGIS build.
  Finishing the migration consistently is worth a dedicated pass.
- **L. SIRENE parquet URL is a single point of failure.** `_load_sirene:1744` hardcodes one
  `object.files.data.gouv.fr/...parquet` path with no fallback; that bucket path has changed
  before. Move it to config (§E) and give a clear, actionable error on 404 instead of a stack
  trace.
- **M. WFS loading blocks the UI.** A full default import serially fires ~15 WFS GetFeature +
  `native:clip` round-trips on the main thread. Wrapping loads in `QgsTask` (or at least
  batching) would keep QGIS responsive on slow connections. Bigger change — only if import
  responsiveness is a felt pain.

---

## 5. Hygiene notes (mostly handled in this cleanup)

- ✅ Removed: 11 editor autosave files, `__pycache__/` (incl. a `.pyc` that was committed to
  git), empty `GEMINI.md`; added a `.gitignore`.
- ✅ Archived to `_archive/`: `fdp_grand_lyon_zae.py` (dead stub) and `install_theme_manager.py`
  (its startup-hook block is auto-installed by `theme_manager.ensure_theme_manager()`; kept as a
  manual fallback). Note the archived installer's docstring hardcodes a `D:\Dropbox\...` path.
- ⚠ **Three live tools are untracked in git:** `fdp_gestionnaire_themes.py`, `fdp_import_lot.py`,
  `fdp_pavillonnaire.py`. They're real, current tools — consider committing them so they're
  versioned like the rest.
- ⚠ **`fdp_pavillonnaire.py` is NOT superseded by `fdp_bati_dans_zone.py`.** They do different
  things: pavillonnaire is an *attribute* filter (residential, ≤2 floors) over the already-built
  `Bâti — Résidentiel` layer; bati-dans-zone is a *spatial* containment test against a polygon
  layer. The tool that generalized into bati-dans-zone was `fdp_grand_lyon_zae` (per its own
  tombstone). Keep pavillonnaire unless you've genuinely stopped using its low-rise-housing view.
