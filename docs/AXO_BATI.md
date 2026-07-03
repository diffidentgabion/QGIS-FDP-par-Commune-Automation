---

# Axonometric Building Volume Tool — Implementation Brief

## Context

Standalone QGIS Processing Toolbox script, saved as `axono_batiments.py` in the same directory as `fdp_par_commune.py`. Operates on any polygon building layer with BD TOPO height attributes — it does not depend on `fdp_par_commune.py` being run first, but is designed to work with the bâti layer it produces.

## What it does

For each building polygon, generates a flat top-down axonometric volume representation as line geometry: floor outline, roof outline, and only the vertical edges that are visible from above (i.e. whose top vertex falls outside the roof polygon).

## Inputs

- **Bâti layer** — polygon layer with `hauteur` (float, metres) and `nombre_d_etages` (int) fields
- **Vertical exaggeration** — float multiplier applied to height, default 1.0
- **Fallback height per floor (m)** — used when `hauteur` is null, multiplied by `nombre_d_etages`, default 3.0
- **Default height (m)** — used when both fields are null or zero, default 3.0

## Output

Memory layer of `QgsMultiLineString` geometries, one feature per input building, same CRS as input. Each feature contains:
- Floor polygon boundary as a linestring
- Roof polygon boundary as a linestring (same XY, Y translated up by `hauteur * exaggeration`)
- Only the vertical edges whose top vertex falls outside the roof polygon geometry

## Algorithm per building

```
1. Get floor polygon vertices via geometry().asPolygon()[0]
2. Skip the last vertex (it duplicates the first in QGIS rings)
3. Compute effective_height:
     if hauteur not null and > 0: use hauteur * exaggeration
     elif nombre_d_etages not null and > 0: use nombre_d_etages * fallback_per_floor * exaggeration
     else: use default_height * exaggeration
4. Build roof vertices: (x, y + effective_height) for each floor vertex
5. Build roof polygon geometry from roof vertices for the inside test
6. For each vertical edge (floor_vertex[i] → roof_vertex[i]):
     if QgsGeometry.fromPointXY(roof_vertex[i]).within(roof_polygon):
         suppress
     else:
         keep
7. Output: floor linestring + roof linestring + kept vertical edges
   as a single QgsMultiLineString feature
```

## Key implementation notes

- Use `asPolygon()[0]` for the exterior ring only — ignore interior rings (courtyards) for now
- `within()` returns True for points on the boundary as well as inside — this is correct behaviour here, boundary vertical edges are silhouette edges and should be kept. Use `contains()` for the test instead so boundary points are kept
- Null/zero height buildings should still output the floor outline only, no roof or verticals, rather than being skipped entirely
- Add a `feedback.setProgress()` call every 100 features and check `feedback.isCanceled()`

## Output styling

Single black `QgsSimpleLineSymbolLayer`, width 0.15mm, applied to the output layer on creation.

## What this tool is NOT

- Not a true 3D axonometric with a view vector or projection matrix
- Not solving occlusion between separate buildings — overlapping lines between adjacent buildings are acceptable
- Not geographically registered in 3D — the Y axis offset is a drawing convention only

---
