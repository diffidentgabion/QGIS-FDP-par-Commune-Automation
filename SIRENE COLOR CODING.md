**Brief: SIRENE Building Color-Coding — `sirene_buildings.py`**

Create a new file `sirene_buildings.py` in the same folder as the main script. It contains a single public function `build_activity_layers` and any private helpers it needs.

**Function signature:**
```python
def build_activity_layers(
    buildings_layer: QgsVectorLayer,
    sirene_layer: QgsVectorLayer,
    feedback,
) -> list[QgsVectorLayer]:
```
Returns an ordered list of `QgsVectorLayer` objects, one per SIRENE category that has at least one matched building. Categories with zero matched buildings are silently omitted. The base grey bâti layer is not modified and not returned — the caller keeps it unchanged.

**The 12 SIRENE categories and their colors must be defined as a module-level constant** so they are easy to maintain and stay in sync with `_apply_sirene_style` in the main script. Define them as a list of dicts:

```python
SIRENE_CATEGORIES = [
    {"label": "Commerce",                          "color": "#F4A261", "naf_ranges": [(45, 47)]},
    {"label": "Restauration & hébergement",        "color": "#E63946", "naf_ranges": [(55, 56)]},
    {"label": "Santé & action sociale",            "color": "#06D6A0", "naf_ranges": [(86, 88)]},
    {"label": "Enseignement",                      "color": "#FFD166", "naf_ranges": [(85, 85)]},
    {"label": "Équipements & services publics",    "color": "#C1121F", "naf_ranges": [(84, 84)]},
    {"label": "Culture, sport & loisirs",          "color": "#118AB2", "naf_ranges": [(90, 93)]},
    {"label": "Services aux personnes & associations", "color": "#F48FB1", "naf_ranges": [(94, 96)]},
    {"label": "Bureaux & services tertiaires",     "color": "#7B2D8B", "naf_ranges": [(58, 66), (68, 75), (77, 82)]},
    {"label": "Industrie, artisanat & construction","color": "#8B5E3C", "naf_ranges": [(5, 9), (10, 43)]},
    {"label": "Transport & logistique",            "color": "#6C757D", "naf_ranges": [(49, 53)]},
    {"label": "Agriculture, sylviculture & pêche", "color": "#2D6A4F", "naf_ranges": [(1, 3)]},
    {"label": "Activité non classée",              "color": "#BBBBBB", "naf_ranges": []},
]
```

The `naf_ranges` field for "Activité non classée" is empty — it acts as a catch-all for SIRENE points whose NAF code doesn't fall into any other category.

**Step-by-step logic:**

**Step 1 — Assign each SIRENE point to its category.**
Iterate over features in `sirene_layer`. For each feature, read `activitePrincipaleEtablissement`, extract the first two characters as an integer (the NAF division), and match it against `SIRENE_CATEGORIES` using the `naf_ranges`. Assign the category index. Points that fail to parse go into "Activité non classée." Store the result as a dict: `{sirene_feature_id: category_index}`.

**Step 2 — Spatial index on buildings.**
Build a `QgsSpatialIndex` from `buildings_layer` for fast lookup. Also build a dict `{building_feature_id: QgsGeometry}` for geometry access without repeated iteration.

**Step 3 — For each SIRENE point, find candidate buildings.**
Use the spatial index to find buildings whose bounding box intersects a 10m buffer around the SIRENE point. For each candidate:
- First test: does the building polygon **contain** the SIRENE point? (`building_geom.contains(sirene_point_geom)`) — if yes, it's a match.
- Second test (fallback): if no containment match was found among all candidates, find the nearest candidate building and check if its distance to the SIRENE point is ≤ 10m. If yes, it's a match.

For each confirmed match, record `(building_feature_id, category_index)` in a `dict[int, set[int]]` — building ID maps to the set of category indices it belongs to. A building can appear in multiple category sets.

**Step 4 — Build one memory layer per category.**
Iterate over `SIRENE_CATEGORIES` by index. For each category that has at least one matched building:
- Create a `QgsVectorLayer` memory layer with geometry type `Polygon` and CRS matching `buildings_layer`.
- Copy all fields from `buildings_layer` to the new layer using `QgsFields`.
- Add the building features (full geometry + all attributes) for every building ID in that category's set.
- Set layer name to `f"Bâti — {category['label']}"`.
- Apply a `QgsSingleSymbolRenderer` with `QgsFillSymbol` using the category color as fill, no outline.
- Append to results list.

**Step 5 — Return the results list** in the same order as `SIRENE_CATEGORIES` (so the legend order is consistent and predictable).

**Integration in the main script:**

At the top of the main script add:
```python
from .sirene_buildings import build_activity_layers, SIRENE_CATEGORIES
```
(Use a relative import since both files are in the same folder.)

In `processAlgorithm`, after the SIRENE layer is loaded and before the layer group is built, add:
```python
if sirene_layer and "buildings" in loaded_layers:
    feedback.pushInfo("Calcul des bâtiments par activité SIRENE…")
    activity_layers = build_activity_layers(
        loaded_layers["buildings"], sirene_layer, feedback
    )
    feedback.pushInfo(f"{len(activity_layers)} couche(s) d'activité générée(s).")
else:
    activity_layers = []
```

In the layer group construction, add a subgroup `"Bâti par activité SIRENE"` inside the commune group, and add all `activity_layers` into it. The base grey `buildings` layer remains in the main group as before.

**Layer group structure after this change:**
```
[Commune name]
├── Établissements SIRENE
├── Bâti par activité SIRENE       ← new subgroup
│   ├── Bâti — Commerce
│   ├── Bâti — Restauration & hébergement
│   └── … (only categories with matches)
├── Bâti                           ← unchanged grey base layer
├── Voirie
└── … (rest unchanged)
```

**Performance note to include as a comment in the code:** For large communes (Paris arrondissements, Lyon) the buildings layer can have 50,000+ features. The `QgsSpatialIndex` approach in Step 2–3 is O(n log n) and should handle this comfortably. If `feedback.isCanceled()` is checked every 500 SIRENE features processed, the user can cancel without hanging QGIS.

**Before writing any code**, answer:
1. The main script uses `processing.run("native:clip")` which returns a memory layer. Does `QgsSpatialIndex` work correctly on memory layers, or does it require a provider-backed layer?
2. For the relative import `from .sirene_buildings import ...` — confirm this works when the script is loaded as a QGIS Processing script (not a plugin). If it doesn't, what is the correct import pattern?
