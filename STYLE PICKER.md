**Brief: Layer Selector Dialog with Style Editing**

Add a `_LayerSelectorDialog` class to the existing script. This dialog fires immediately after commune selection and before any data loading. It replaces the hardcoded `wfs_definitions` list — the user's choices become the layer list driving all subsequent loading.

**Dialog layout:**

Two-panel layout using `QSplitter`:
- Left panel: layer catalogue organised in three `QGroupBox` sections (scrollable if needed)
- Right panel: order list + style editor for the selected layer

**Left panel — layer catalogue:**

Three sections as `QGroupBox` with checkboxes:

*Section 1 — "Couches par défaut"* (all pre-checked): the current 8 layers plus SIRENE — commune boundary, parcelles, cours d'eau, surface hydrographique, voirie, voie ferrée, bâti, végétation, établissements SIRENE. Add `BDTOPO_V3:aerodrome` and `BDTOPO_V3:piste_d_aerodrome`.

*Section 2 — "Couches recommandées"* (all unchecked): `BDTOPO_V3:erp` → "ERP", `BDTOPO_V3:construction_surfacique` → "Constructions surfaciques", `BDTOPO_V3:itineraire_autre` → "Itinéraires (vélo, pédestre)", `BDTOPO_V3:haie` → "Haies", `BDTOPO_V3:cimetiere` → "Cimetières", `BDTOPO_V3:equipement_de_transport` → "Équipements de transport", `BDTOPO_V3:detail_hydrographique` → "Détails hydrographiques", `BDTOPO_V3:foret_publique` → "Forêts publiques".

*Section 3 — "Couches avancées"* (collapsed by default, all unchecked): , `BDTOPO_V3:canalisation`, `BDTOPO_V3:construction_lineaire`, `BDTOPO_V3:construction_ponctuelle`, `BDTOPO_V3:detail_orographique`, `BDTOPO_V3:lieu_dit_non_habite`, `BDTOPO_V3:point_de_repere`, `BDTOPO_V3:pylone`, `BDTOPO_V3:reservoir`, `BDTOPO_V3:terrain_de_sport`, `BDTOPO_V3:zone_d_activite_ou_d_interet`.

**Right panel — order and style:**

Top half: the active layer order list (`QListWidget`, non-editable text) showing only checked layers in current order, with Up / Down buttons. Reorders live as user checks/unchecks or clicks arrows. This order becomes the QGIS legend order.

Bottom half: style editor that updates when the user clicks a layer in the order list. The style editor shows different controls depending on geometry type:

For **polygon layers** (commune boundary, parcelles, bâti, végétation, water surface, construction_surfacique, cimetiere, foret_publique, terrain_de_sport, zone_d_activite_ou_d_interet, erp, reservoir): show fill color picker (with opacity/alpha slider), outline color picker, outline width spinbox (0.0–5.0, step 0.1 mm), outline style toggle (solid / dashed / none).

For **line layers** (cours d'eau, voirie, voie ferrée, canalisation, construction_lineaire, haie, itineraire_autre): show line color picker, line width spinbox (0.0–5.0, step 0.1 mm), line style toggle (solid / dashed).

For **point layers** (SIRENE, construction_ponctuelle, detail_hydrographique, equipement_de_transport, detail_orographique, lieu_dit_non_habite, point_de_repere, pylone, piste_d_aerodrome, aerodrome): show marker color picker and marker size spinbox (0.5–10.0, step 0.5 mm). No line style control for points.

Each layer's geometry type must be declared in the layer catalogue definition (not inferred at runtime) so the correct controls appear before any data is loaded.

Color pickers use `QgsColorButton` (the native QGIS color button widget, which includes alpha support) rather than raw `QColorDialog` — it integrates better with the QGIS UI.

All style fields are pre-populated with the layer's current default values from `_apply_style` so the user sees the existing style as a starting point.

A "Réinitialiser" button per layer resets its style to the coded defaults.

**Return value:**

On `accept()`, the dialog returns an ordered list of dicts, one per checked layer:
```python
{
    "typename": "BDTOPO_V3:batiment",   # or None for SIRENE
    "display_name": "Bâti",
    "style_key": "buildings",
    "geom_type": "polygon",             # "polygon" | "line" | "point"
    "style": {
        # polygon
        "fill_color": QColor,
        "fill_opacity": float,          # 0.0–1.0
        "outline_color": QColor,
        "outline_width": float,
        "outline_style": str,           # "solid" | "dashed" | "none"
        # line
        "line_color": QColor,
        "line_width": float,
        "line_style": str,
        # point
        "marker_color": QColor,
        "marker_size": float,
    }
}
```

**Integration into `_apply_style`:**

Add a companion method `_apply_custom_style(layer, style_dict)` that reads the dict above and applies it using the same `QgsSingleSymbolRenderer` / `QgsFillSymbolLayer` / `QgsLineSymbolLayer` / `QgsMarkerSymbolLayer` approach already in the script. In `processAlgorithm`, after loading each layer, call `_apply_custom_style` instead of `_apply_style` when a custom style dict is present, falling back to `_apply_style` for SIRENE (which uses rule-based rendering and should not be overridden by this simple editor).

**Integration point in `processAlgorithm`:**

Replace the hardcoded `wfs_definitions` list with a call to `_LayerSelectorDialog`. If the user cancels the dialog, raise an exception to halt the algorithm cleanly with the message "Sélection des couches annulée."

**Before writing any code**, answer the following:
1. How will the Section 3 "Couches avancées" collapsed state be implemented in PyQt5 — `QGroupBox` does not natively support collapsing, so what is your approach?
2. How will the order list stay in sync with the checkboxes — specifically, what signal/slot connections will manage adding and removing items from the order list when a checkbox is checked or unchecked?
3. `QgsColorButton` requires a `QgsColorSchemeRegistry` — confirm it is available in a standard QGIS 3.28 processing context and show the import.
