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

## Theme-first import (`THEME_FIRST`, v6)

By default (`THEME_FIRST = true`, config at the top of the `.jsx`) the importer
**transposes** the tree before building: instead of mirroring commune→theme, it
creates one top-level **master layer per theme** with one sublayer per commune
inside, each keeping its full original sub-structure:

```
Topographie                ← master
  Fontainebleau            ← commune sublayer (original sub-structure inside)
  Barbizon
Voirie
  Fontainebleau
    Autoroute
    Route imp. 1 …
  Barbizon
…
```

Rules (implemented in `maybeTransposeThemeFirst()` / `transposeThemeFirst()`):

- **Only commune groups are regrouped** (v6.3). Exporter v6 tags them with
  `"insee"` in the manifest (from the QGIS custom property `fdp_insee`);
  tagged groups regroup automatically, everything else — loose layers, Axo
  stacks, any untagged group — keeps its exact position and internal order.
  On old manifests with no tags the importer ASKS before regrouping; "No"
  imports the tree exactly as exported. Never regroup silently: merging
  same-named sibling groups (an Axo per-building stack) destroys their
  painter's-algorithm draw order.
- Themes are matched across communes by **exact name** at the
  commune-group-child level (names are stable there; variable names like
  `Courbes de niveau LiDAR HD (5 m)` live deeper inside stable groups).
- Master order = first commune's theme order (QGIS legend order); themes first
  seen in a later commune are appended at the bottom. The masters block sits
  at the position of the first commune group.
- Commune order inside each master = manifest root order.
- A leaf theme (single PDF, e.g. `Commune`) yields a commune sublayer that
  holds the placed art directly.
- The same theme may be a group in one commune (rule-based renderer) and a
  leaf in another (older export) — both merge under one master.

Set `THEME_FIRST = false` to disable regrouping entirely.

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
