**Context:** The working codebase consists of `fdp_par_commune.py`, `sirene_buildings.py`, and `zone_buildings.py`. We are adding a new `bati_buildings.py` file and making minimal targeted additions to `fdp_par_commune.py` only. Do not touch `sirene_buildings.py` or `zone_buildings.py`.

---

**New file `bati_buildings.py`**

Single public function:

```python
def build_bati_layers(
    buildings_layer: QgsVectorLayer,
    feedback,
) -> list[QgsVectorLayer]:
```

Returns an ordered list of `QgsVectorLayer` objects. All layers are memory polygon layers with the same CRS and full attribute set copied from `buildings_layer`.

Import `generate_gradient` from `zone_buildings.py` using the same import pattern already established in the project.

---

**Classification logic — iterate once over all building features and sort each into buckets:**

Read the following fields for each feature. Normalise all string fields to stripped lowercase for comparisons. Treat null numeric fields as 0.

Fields used: `nombre_de_logements` (integer), `usage_1` (string), `nature` (string), `nombre_d_etages` (integer).

**Bucket assignment rules, applied in strict priority order — first match wins:**

1. **Résidentiel** — if `nombre_de_logements >= 1`

2. **Religieux** — else if `nature` is one of: `"église"`, `"chapelle"`. Style matches ZAI Religieux base color `#9B72AA`.

3. **Château** — else if `nature` is `"château"`. Style matches ZAI Administratif & militaire base color `#C1121F` (châteaux in French urbanisme are typically classified as patrimoine under administrative/cultural heritage — use this color with a note in the code explaining the choice).

4. **Autre industriel** — else if `usage_1` is `"industriel"` OR `nature` is `"industriel, agricole ou commercial"`. Style matches ZAI Industriel & commercial base color `#8B5E3C`.

5. **Non classé** — all remaining buildings. Neutral grey `#AAAAAA`, no outline. These are buildings the BD TOPO has insufficient data to classify.

Each bucket generates one memory layer. Empty buckets are silently omitted. Layer names: `"Bâti — Résidentiel"`, `"Bâti — Religieux"`, `"Bâti — Château"`, `"Bâti — Industriel"`, `"Bâti — Non classé"`.

Style for Résidentiel: solid fill `#C0C0C0` (same grey as the base bâti layer), no outline.
Style for all others: as specified above, no outline.

---

**Supplemental statistics layers — generated from the same single iteration:**

Alongside the classification buckets, accumulate two additional feature lists during the same loop:

**List A — residential density features:**
For every feature that goes into the Résidentiel bucket, compute:
```python
density = nombre_de_logements / max(nombre_d_etages, 1)
```
Store the feature and its computed density value.

**List B — height features:**
For every building feature regardless of classification bucket, compute:
```python
floors = nombre_d_etages if nombre_d_etages >= 1 else 1
```
Store the feature and its floors value.

After the iteration loop, generate the two statistics layers:

**Density layer — `"Bâti — Densité résidentielle"`:**

Find `max_density` across all features in List A. Apply a `QgsGraduatedSymbolRenderer` with 7 classes using `EqualInterval` mode from 0 to `max_density`. Generate 7 colors using `generate_gradient("#FF0000", 7)` — lightest red for lowest density, darkest red for highest. Add a new field `"densite_log"` (Double) to the layer to store the computed value. This field drives the graduated renderer. No outline.

**Height layer — `"Bâti — Hauteur (étages)"`:**

Find `max_floors` across all features in List B, capped at 20 for gradient purposes (buildings above 20 floors get the darkest shade but are not excluded). Apply a `QgsGraduatedSymbolRenderer` with classes from 1 to `min(max_floors, 20)`, one class per floor count, using `EqualInterval`. Generate colors using `generate_gradient("#C0C0C0", min(max_floors, 20))` — lightest grey for 1 floor (matching the base bâti color), darkest grey for the maximum. Add a new field `"nb_etages_norm"` (Integer) storing the normalised floor value (null/0 → 1, above 20 → 20). No outline.

For both statistics layers, use `QgsGraduatedSymbolRenderer` constructed programmatically with `QgsRendererRange` objects. Do not use `QgsVectorLayer.setRenderer` with a string expression — build the ranges explicitly from the computed min/max values so the renderer works correctly on memory layers without requiring a separate field expression evaluation pass.

Check `feedback.isCanceled()` every 500 features during the main iteration loop.

---

**Integration in `fdp_par_commune.py`**

Add import at top:
```python
from bati_buildings import build_bati_layers
```

After the zone buildings block and before group construction, add:

```python
if "buildings" in loaded_layers:
    feedback.pushInfo("Classification intrinsèque du bâti…")
    bati_layers = build_bati_layers(loaded_layers["buildings"], feedback)
    feedback.pushInfo(f"{len(bati_layers)} couche(s) bâti intrinsèque générée(s).")
else:
    bati_layers = []
```

In the layer group construction, add a `"Bâti intrinsèque"` subgroup alongside `"Bâti par activité SIRENE"` and `"Bâti par zone d'activité"`. Add all `bati_layers` into it. The two statistics layers (`"Bâti — Densité résidentielle"` and `"Bâti — Hauteur (étages)"`) go at the top of this subgroup, above the classification layers, since they are the most likely to be toggled on and off independently during analysis.

**Updated layer group structure:**

```
[Commune name]
├── Établissements SIRENE
├── Bâti par activité SIRENE
│   └── … (unchanged)
├── Bâti par zone d'activité
│   └── … (unchanged)
├── Bâti intrinsèque                         ← new subgroup
│   ├── Bâti — Densité résidentielle         ← red gradient
│   ├── Bâti — Hauteur (étages)              ← greyscale gradient
│   ├── Bâti — Résidentiel
│   ├── Bâti — Religieux
│   ├── Bâti — Château
│   ├── Bâti — Industriel
│   └── Bâti — Non classé
├── Zones d'activité et d'intérêt
├── Bâti                                     ← unchanged grey base
├── Voirie
└── …
```

---

**Before writing any code**, answer the following:

1. `QgsGraduatedSymbolRenderer` on a memory layer requires that the field driving the classification exists in the layer's field schema before features are added. Confirm the exact sequence: field added to schema → features added with that field populated → renderer applied. Show the three PyQGIS calls in order.

2. For the height layer, buildings with `nombre_d_etages` null or 0 are normalised to 1 floor and receive the lightest grey — confirm this means they are visually indistinguishable from genuine single-storey buildings, and state whether a comment should note this limitation in the code.

3. The `nature` field values in BD TOPO (`"église"`, `"château"` etc.) — confirm whether they arrive from the WFS in lowercase, titlecase, or mixed, and show how the normalisation handles this before the bucket assignment comparisons.
