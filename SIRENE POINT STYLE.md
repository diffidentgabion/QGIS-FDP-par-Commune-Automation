**Context:** Working codebase: `fdp_par_commune.py`, `sirene_buildings.py`, `zone_buildings.py`, `bati_buildings.py`. We are adding a new `sirene_display.py` file and making minimal targeted additions to `fdp_par_commune.py` only. Do not touch any other existing file.

---

**New file `sirene_display.py`**

Single public function:

```python
def build_displaced_sirene_layer(
    sirene_layer: QgsVectorLayer,
    buildings_layer: QgsVectorLayer,
    feedback,
) -> QgsVectorLayer:
```

Returns a single `QgsVectorLayer` of type Point, CRS matching `sirene_layer`, containing all SIRENE features repositioned to displaced coordinates around their matched building centroid. The returned layer replaces the original SIRENE layer in the legend — the original is not added to the project.

Import `generate_gradient` from `zone_buildings` and `SIRENE_CATEGORIES` from `sirene_buildings` using the established import pattern.

---

**Step 1 — Build category order lookup**

From `SIRENE_CATEGORIES`, build a dict `category_order: dict[str, int]` mapping each category label to its index in the list. This defines the angular sort order of points around the circle — index 0 starts at the top (north, 90° in standard trigonometry) and proceeds clockwise.

Also replicate the NAF-to-category matching logic from `sirene_buildings.py` as a local helper:

```python
def _get_category_index(naf_code: str) -> int:
```

Returns the index into `SIRENE_CATEGORIES` for a given NAF code string, or `len(SIRENE_CATEGORIES) - 1` (the catch-all) if no match. Use the same `naf_ranges`, `naf_exclude_suffixes`, and `naf_exact_codes` logic already established.

---

**Step 2 — Spatial index and centroid lookup on buildings**

Build a `QgsSpatialIndex` on `buildings_layer`. Build a dict `building_centroids: dict[int, QgsPointXY]` mapping each building fid to its polygon centroid (`geom.centroid().asPoint()`). Build a dict `building_geoms: dict[int, QgsGeometry]` for containment testing.

---

**Step 3 — Match each SIRENE point to a building centroid**

For each feature in `sirene_layer`:

- Get the point geometry as `QgsPointXY`
- Query spatial index for candidate buildings within a 10m search buffer (consistent with `sirene_buildings.py`)
- First test containment: if the point falls inside a building polygon, use that building's centroid
- Fallback: find the nearest candidate building within 10m, use its centroid
- If no building match within 10m: use the original SIRENE point coordinates unchanged — do not discard unmatched points

Store results as `point_to_centroid: dict[int, QgsPointXY]` mapping sirene fid → anchor point (either building centroid or original coords).

Check `feedback.isCanceled()` every 500 features.

---

**Step 4 — Group points by anchor**

Group SIRENE feature fids by their anchor point. Since `QgsPointXY` is not hashable, use a string key: `f"{anchor.x():.2f},{anchor.y():.2f}"` (2 decimal places is sufficient precision in EPSG:2154 metres). Result: `groups: dict[str, list[int]]` mapping anchor key → list of sirene fids sharing that anchor.

For each group, sort the fid list by `_get_category_index(naf_code)` ascending so points are arranged clockwise from north in legend order.

---

**Step 5 — Compute displaced positions**

For each group:

- If the group has exactly 1 point: displaced position = anchor point exactly (no displacement)
- If the group has n > 1 points: distribute evenly on a circle around the anchor

**Displacement radius:** do not bake a fixed map-unit radius into the coordinates. Instead, store the anchor point as the feature geometry, and encode the displacement as a **data-defined symbol offset** using two new fields `"offset_x_mm"` and `"offset_y_mm"` (Double) in millimetres. Compute the offset for point i of n as:

```python
import math
RADIUS_MM = 4.0  # millimetres at screen — scales naturally with zoom
angle = math.pi / 2 - (2 * math.pi * i / n)  # start north, go clockwise
offset_x = RADIUS_MM * math.cos(angle)
offset_y = RADIUS_MM * math.sin(angle)
```

For a lone point: `offset_x = 0.0`, `offset_y = 0.0`.

`RADIUS_MM = 4.0` should be defined as a module-level constant so it is easy to adjust.

---

**Step 6 — Build output layer**

Create a memory Point layer with CRS matching `sirene_layer`. Copy all fields from `sirene_layer` plus add:

```python
QgsField("offset_x_mm", QVariant.Double)
QgsField("offset_y_mm", QVariant.Double)
QgsField("category_index", QVariant.Int)
```

For each SIRENE feature, create a new feature with:
- Geometry: the anchor point (building centroid or original coords)
- All original attributes copied
- `offset_x_mm` and `offset_y_mm` populated from Step 5
- `category_index` populated from `_get_category_index`

Add all features in one `dataProvider().addFeatures()` call per group for efficiency.

---

**Step 7 — Apply renderer**

Apply the same `QgsRuleBasedRenderer` structure as `_apply_sirene_style` in `fdp_par_commune.py` — identical category rules, colors, marker shapes and sizes. The key addition is data-defined offset on every symbol.

For each `QgsMarkerSymbol` in each rule, set data-defined properties:

```python
from qgis.core import QgsProperty

symbol.setDataDefinedProperty(
    QgsSymbolLayer.PropertyOffset,
    QgsProperty.fromExpression(
        'array("offset_x_mm", "offset_y_mm")'
    )
)
```

This tells QGIS to read the per-feature offset values and apply them in millimetres at render time, so the visual displacement scales naturally with zoom while the underlying geometry stays at the anchor point.

Also set `symbol.setSizeUnit(QgsUnitTypes.RenderMillimeters)` and `symbol.setOffset(QgsPointXY(0, 0))` to ensure the data-defined offset is the sole source of displacement.

Layer name: `"Établissements SIRENE (déplacés)"`.

---

**Step 8 — Sync with `_apply_sirene_style`**

Add a comment in `sirene_display.py` noting that the category rules, colors, marker shapes and sizes must stay in sync with `_apply_sirene_style` in `fdp_par_commune.py`. If `SIRENE_CATEGORIES` is ever updated, both renderers need updating. A future refactor could extract the symbol factory into a shared helper — flag this as a TODO.

---

**Integration in `fdp_par_commune.py`**

Add import at top:
```python
from sirene_display import build_displaced_sirene_layer
```

In `processAlgorithm`, after the SIRENE layer is loaded and validated (after the existing `if sirene_layer:` block) and before the `build_activity_layers` call, replace the `sirene_layer` variable with the displaced version:

```python
if sirene_layer and "buildings" in loaded_layers:
    feedback.pushInfo("Calcul des positions SIRENE déplacées…")
    sirene_layer = build_displaced_sirene_layer(
        sirene_layer,
        loaded_layers["buildings"],
        feedback,
    )
```

This means all downstream code — `build_activity_layers`, the layer group construction, `_apply_sirene_style` — receives the displaced layer transparently. No other changes needed in the main script.

**Updated legend:** The layer `"Établissements SIRENE (déplacés)"` replaces `"Établissements SIRENE"` at the top of the commune group. Everything else unchanged.

---

**Before writing any code**, answer the following:

1. `QgsSymbolLayer.PropertyOffset` expects an array of two values `[x, y]` in the data-defined expression. Confirm the correct QGIS expression syntax for reading two separate fields as an offset array, and confirm this works with `RenderMillimeters` unit mode in QGIS 3.28.

2. The anchor geometry stored in the output layer is `QgsPointXY` (the building centroid). When multiple SIRENE features share the same anchor, they will have identical geometries but different offset values. Confirm that `QgsSpatialIndex` and QGIS rendering handle duplicate point geometries correctly — no features will be silently dropped.

3. `RADIUS_MM = 4.0` is fixed regardless of how many points share an anchor. For a building with 10+ establishments the circle will be very tight. Should the radius scale with the number of points in the group — e.g. `RADIUS_MM = max(4.0, n * 1.2)` — or stay fixed and let the user adjust the constant manually?