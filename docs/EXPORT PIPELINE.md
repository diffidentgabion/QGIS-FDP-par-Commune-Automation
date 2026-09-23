# Export pipeline — QGIS → PDF → Illustrator

Two scripts, both living in `export/` (single home, no copies elsewhere).
Both are location-independent: the exporter is pasted into the QGIS Python
Console, the importer is browsed to via File > Scripts > Other Script…

## Workflow

1. Build the commune(s) in QGIS with `fdp_par_commune.py` / `fdp_import_lot.py`
   — one layer-tree group per commune, identical theme structure inside.
2. Paste `export/qgis_export_layers.py` into the QGIS Python Console and run.
   It exports **one flat PDF per leaf layer** (or per named renderer rule) into
   a `layer_pdfs/` folder next to the `.qgz`, plus a `manifest.json` describing
   the tree. Every PDF shares the same page size and extent, so they stack in
   register. Canvas rotation is **baked into the PDFs** here via
   `setMapRotation()` — the Illustrator script must never rotate again.
3. In Illustrator: File > Scripts > Other Script… > `qgis_to_illustrator.jsx`,
   select the `manifest.json`. The script creates a new document and places
   every PDF on its own layer (scaled to artboard width, embedded).

## manifest.json

```json
{
  "canvas_width_px": ..., "canvas_height_px": ...,
  "page_width_mm": 400.0, "page_height_mm": ...,
  "canvas_rotation": ..., "dpi": 300,
  "children": [
    { "type": "group", "name": "<Commune>", "children": [
        { "type": "group", "name": "Topographie", "children": [
            { "type": "layer", "name": "Courbes de niveau LiDAR HD (5 m)",
              "file": "0001_....pdf" } ] },
        { "type": "layer", "name": "Commune", "file": "..." },
        ...
    ]},
    ...
  ]
}
```

Child order = QGIS legend order top→bottom. A rule-based vector layer appears
as a synthetic group named after the layer, one child per named rule.
Filenames are ASCII-lossy (`Végétation` → `V_g_tation`) — always match nodes
by `name`, never by `file`.

## Thematic import (`THEME_FIRST`, v6 → v7)

By default (`THEME_FIRST = true`, config at the top of the `.jsx`) the importer
**merges** the communes before building: instead of mirroring commune→theme,
every commune's subtree is folded into one shared theme tree, matching nodes
by name **at every depth** (v7). No commune appears at any level; a leaf
layer holds one placed PDF per commune:

```
Topographie
  Courbes de niveau LiDAR HD (5 m)   ← one layer, N placed PDFs
Voirie
  Autoroute
  Route imp. 1 …
Commune                              ← all commune limits on one layer
…
```

Per-commune selection in Illustrator relies on the commune limits layer.
v6–v6.4 produced `Theme → Commune → content` (and `Theme → CTM → Commune`
for nested layouts); that kept communes visible at every level and was
dropped in v7.

Rules (implemented in `maybeTransposeThemeFirst()` / `transposeLevel()` /
`mergeInto()`):

- **Only commune groups are regrouped** (v6.3). Exporter v6 tags them with
  `"insee"` in the manifest (from the QGIS custom property `fdp_insee`);
  tagged groups regroup automatically, everything else — loose layers, Axo
  stacks, any untagged group — keeps its exact position and internal order.
  On old manifests with no tags the importer ASKS before regrouping; "No"
  imports the tree exactly as exported. Never regroup silently: merging
  same-named sibling groups (an Axo per-building stack) destroys their
  painter's-algorithm draw order.
- **Commune groups may be nested** (v6.4) inside organisational groups the
  user creates in QGIS (e.g. CTMs in Lyon: root → CTM → communes). The
  importer finds tagged communes at any depth and merges them into the same
  themes; since v7 the organisational level disappears too. Anything else in
  an organisational group (a loose CTM boundary layer…) stays in a group of
  the same name right after the themes block. Before v6.4 only top-level
  groups were inspected: a CTM layout fell through to the untagged-manifest
  prompt, and "Yes" treated each CTM as a commune, so the "themes" came out
  as commune names.
- Nodes match across communes by **type + exact name**, recursively. A name
  that varies between communes (e.g. `Courbes de niveau LiDAR HD (5 m)` vs a
  different step) just yields two sibling layers.
- Same-named siblings *within* one commune are never collapsed: the k-th
  occurrence matches the k-th occurrence in the merged tree.
- Order = first commune's legend order; a node first seen in a later commune
  is inserted right after the node it followed there. The themes block sits
  at the position of the first commune group.
- A group and a leaf with the same name (rule-based renderer in one commune,
  plain layer in another) stay two separate sibling layers.
- Missing/failed PDFs are reported per commune (`Layer (Commune)`) in the
  final alert; the layer itself is suffixed `[PDF missing]` / `[place failed]`.

Set `THEME_FIRST = false` to disable regrouping entirely.

## Global swatches (`BUILD_SWATCHES`, v7.1)

After each leaf layer is placed (all communes), the importer walks its
embedded art and turns every distinct fill/stroke colour into a **global
process swatch**, then links every path using that colour to it. Editing the
swatch recolours that layer everywhere in the drawing, all communes at once.

```
Swatches panel
  Voirie                       ← swatch group = top-level theme
    Autoroute · trait
    Autoroute · fond
    Route imp. 1
  Bâti par activité SIRENE
    Commerce
    …
```

- Colours come from the **embedded art itself**, not from the QGIS renderer:
  the PDF already is the QGIS appearance, so nothing is matched or parsed and
  nothing can drift between the two. No exporter change was needed.
- One swatch per distinct colour **per layer** (RGB rounded to integers): two
  layers sharing a colour get separate swatches, so they can diverge. A layer
  with one colour gets the layer name; with several, `· fond` / `· trait`
  (+ number) after the role of first use.
- Names are unique document-wide: on a clash the parent group is prefixed
  (`Fer / Autoroute`), then a number is appended.
- Left untouched: clipping paths, pattern/gradient fills, rasters, anything
  not RGB or gray. QGIS layouts export labels as outlines by default, so
  labels are recoloured like any other path; live text frames are not.
- Layers with more than `MAX_SWATCHES_PER_LAYER` (12) colours, e.g. graduated
  ramps, are left as is and listed in the final alert. Per-layer errors are
  caught and listed the same way; they never abort the import.
- Requires embedded placement: skipped when `PLACE_AS_LINKED = true`.
- Cost: every path is read and relinked once, which adds time on dense
  building layers. Set `BUILD_SWATCHES = false` if an import is too slow.

## History / gotchas

- **"The framing changed" is almost always the export, not the import.** The
  PDF framing = the QGIS canvas extent at the moment the exporter ran
  (`PADDING = 0`); zooming/panning the canvas between exports changes the
  framing baked into every PDF. Check the `manifest.json` timestamp before
  suspecting the importer — `layer_pdfs/` accumulates files from several
  export runs, only the ones referenced by the current manifest matter.
- **Sticky "Crop to" Place option (guard since v6.2).** Illustrator remembers
  the "Crop to" choice from the last manual File > Place with import options
  and silently applies it to *scripted* placement. Anything but **Media Box**
  crops each PDF to its content, so layers scale on their own bounds and fall
  **out of register**. The importer checks every placed PDF's native width
  against `page_width_mm` and aborts with instructions on mismatch. Manual
  fix: File > Place any layer PDF with "Show Import Options", set Crop to
  "Media Box", place, undo, rerun. The script never writes Place preferences
  itself: v6.1 briefly wrote the undocumented "plugin/PDFImport/CropTo" key
  with a guessed value, which can itself set a wrong crop mode — do not
  reintroduce that.

- **v6** removed a dead rotation block in `placePDF`: it referenced an
  out-of-scope variable, so it had never executed — which was correct, because
  rotation is baked into the PDFs at export time. Re-adding rotation in
  Illustrator would double-rotate.
- The `.jsx` uses a hand-rolled iterative JSON parser because ExtendScript's
  `eval()` blows its ~256-frame stack on deeply nested JSON. ExtendScript is
  ES3: no `JSON`, no `Array.indexOf/map/filter`.
