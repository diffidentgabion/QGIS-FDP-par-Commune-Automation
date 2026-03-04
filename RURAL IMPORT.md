**Context:** Working codebase, minimal targeted changes to `fdp_par_commune.py` only. No new files needed. Do not touch any other existing file.

---

**Change 1 — RPG layer definitions**

Add the following entries to `_LAYER_CATALOGUE` (the existing list of dicts). All RPG layers use `"checked": False` (disabled by default). No structural change to the dict schema is needed — `"checked"` already serves as the enable/disable flag; the loading loop already gates on it via the layer-selector dialog.

Insert this new group above the `CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle` entry so RPG layers render below cadastral parcels but above the commune boundary:

```python
# ── Couches rurales RPG (désactivées par défaut) ──────────────────────────
# Typenames vérifiés sur GetCapabilities data.geopf.fr — 2025-03-04.
# RPG.LATEST:parcelles_graphiques et RPG.LATEST:ilots_anonymes ont un alias
# LATEST stable. Les quatre autres sont épinglés à 2024 et devront être mis
# à jour lors de la publication de l'édition 2025.
{"section": "rural", "typename": "RPG.LATEST:parcelles_graphiques",
 "display_name": "RPG — Parcelles agricoles",   "style_key": "rpg_parcelles",
 "geom_type": "polygon", "checked": False},
{"section": "rural", "typename": "RPG.LATEST:ilots_anonymes",
 "display_name": "RPG — Îlots",                 "style_key": "rpg_ilots",
 "geom_type": "polygon", "checked": False},
# ⚠ Typename 2024-pinned — mettre à jour pour l'édition 2025 :
{"section": "rural",
 "typename": "IGNF_RPG_PARCELLES-AGRICOLES-CATEGORISEES_2024:parcelles_agricole_categorisees_2024",
 "display_name": "RPG — Catégories PAC",        "style_key": "rpg_pac",
 "geom_type": "polygon", "checked": False},
{"section": "rural",
 "typename": "IGNF_RPG_PRAIRIES-PERMANENTES_2024:prairies_permanentes_2024",
 "display_name": "RPG — Prairies permanentes",  "style_key": "rpg_pp",
 "geom_type": "polygon", "checked": False},
{"section": "rural",
 "typename": "IGNF_RPG_PARCELLES-ELIGIBLES-IAE:parcelles_eligibles_iae_2024",
 "display_name": "RPG — Infra. agro-env.",      "style_key": "rpg_iae",
 "geom_type": "polygon", "checked": False},
# ⚠ Typename 2024-pinned avec date dans le nom local — mettre à jour pour 2025 :
{"section": "rural",
 "typename": "IGNF_RPG_ZONES-DENSITE-HOMOGENE_2024:surfaces_2024_zdh_20250621",
 "display_name": "RPG — Zones densité homogène","style_key": "rpg_zdh",
 "geom_type": "polygon", "checked": False},
```

Also add stub entries to `_DEFAULT_STYLES` for all six RPG keys (value `None` — styled via dedicated methods, not the generic editor):

```python
"rpg_parcelles": None,
"rpg_ilots":     None,
"rpg_pp":        None,
"rpg_pac":       None,
"rpg_iae":       None,
"rpg_zdh":       None,
```

**Layers dropped from the original brief:** BIO (WFS endpoint does not exist — WMTS/raster only) and SNA (no WFS endpoint found).

---

**Change 2 — Layer group structure**

In the layer group construction block, detect whether any RPG layers were loaded and if so create a `"Couches rurales RPG"` subgroup inside the commune group, positioned above `"Parcelles cadastrales"`. Add all loaded RPG layers into this subgroup. If no RPG layers were loaded, skip the subgroup entirely.

The detection set: `{"rpg_parcelles", "rpg_ilots", "rpg_pac", "rpg_pp", "rpg_iae", "rpg_zdh"}`.

The subgroup insertion point: after the loop processes the `parcels` (`CADASTRALPARCELS`) entry — insert the RPG group just before it so it appears above in the legend.

---

**Change 3 — Vegetation subcategory styling**

*(Deferred — implement after RPG layers are working.)*

---

**Change 4 — RPG style methods**

Add the following style methods to `FDPParCommune`. All fills solid, no outline unless stated, ELSE fallback neutral light grey `#E8E8E8`. Route all six RPG style keys through `_apply_style` by adding `elif` branches there (same pattern as `sirene` and `zai`).

---

**`_apply_rpg_parcelles_style(layer)`**

Rule-based on `code_cultu` (confirmed field name from DescribeFeatureType). Use `starts_with("code_cultu", 'XX')` expressions in QGIS filter syntax. Culture group mapping:

| Group | Codes (starts-with prefixes) | Color |
|---|---|---|
| Céréales | BLE, BTH, BDH, ORH, ORG, MAI, SGL, TRI, MIL, SOR, AVE | `#F5DEB3` |
| Oléagineux & protéagineux | TOU, COL, LIN, POI, FEV, LUZ, SBO | `#DAA520` |
| Prairies temporaires | PPH, PTR, PRL, PEX | `#90EE90` |
| Prairies permanentes | PPE, PPC | `#228B22` |
| Vignes | VIG | `#722F37` |
| Vergers | ARB, AGR | `#FF8C00` |
| Maraîchage & légumes | LEG, MEL, CHP, ASP, ART | `#98FB98` |
| Jachères | JAC | `#D2B48C` |
| Cultures industrielles | BTR, CHC, TAB | `#CD853F` |
| Surfaces boisées | BOI, FOR, MIX | `#355E3B` |
| Autres | ELSE | `#E8E8E8` |

Note: `LIN` appears in both Oléagineux (lin oléagineux) and Cultures industrielles (lin textile) — keep it only in Oléagineux since lin oléagineux is more common in RPG.

---

**`_apply_rpg_ilots_style(layer)`**

Single neutral fill `#F5F0E8`, thin `#AAAAAA` 0.3 px outline. Îlots are anonymised farm block boundaries — render as a subtle grid, no category differentiation.

---

**`_apply_rpg_pac_style(layer)`**

Rule-based. Fetch the field name from DescribeFeatureType on `IGNF_RPG_PARCELLES-AGRICOLES-CATEGORISEES_2024:parcelles_agricole_categorisees_2024` before coding the filter expression. Expected PAC category values:

| Valeur | Label | Color |
|---|---|---|
| TA — Terres arables | Terres arables | `#F5DEB3` |
| CP — Cultures permanentes | Cultures permanentes | `#FF8C00` |
| PP — Prairies permanentes | Prairies permanentes | `#228B22` |
| SB — Surfaces boisées | Surfaces boisées | `#355E3B` |

---

**`_apply_rpg_pp_style(layer)`**

Single solid fill `#52B788`, no outline. Label "Prairies permanentes".

---

**`_apply_rpg_iae_style(layer)`**

Rule-based on IAE element type. Fetch the field name from DescribeFeatureType on `IGNF_RPG_PARCELLES-ELIGIBLES-IAE:parcelles_eligibles_iae_2024` before coding the expression. Expected values: haies → `#52B788`, bandes enherbées → `#90EE90`, bosquets → `#2D6A4F`, mares → `#6baed6`, arbres isolés → `#1B4332`. ELSE `#74C69D`.

---

**`_apply_rpg_zdh_style(layer)`**

Rule-based or graduated on the density/homogeneity field. Fetch the field name from DescribeFeatureType on `IGNF_RPG_ZONES-DENSITE-HOMOGENE_2024:surfaces_2024_zdh_20250621` before coding. Use a sequential yellow→brown ramp (`#FFFDE7` → `#F9A825` → `#E65100`) if the field is numeric density, or neutral fills `#F5F0E8` / `#D4C9A8` / `#A89070` across categories if categorical. ELSE `#E8E8E8`.

---

**Change 5 — Layer selector dialog**

In `_LayerSelectorDialog._build_ui()`, add a fourth section call after the existing three:

```python
self._build_section(scroll_layout, "Couches rurales RPG", "rural", collapsible=True)
```

Use `collapsible=True` (closed by default, same as "Couches avancées"). Add a small grey informational label immediately after the group header inside `_build_section` when `section == "rural"`:

> *Ces couches sont recommandées pour les communes rurales.*

The six layers listed will be: Parcelles agricoles, Îlots, Catégories PAC, Prairies permanentes, Infra. agro-env., Zones densité homogène — all unchecked by default (already handled by `"checked": False` in the catalogue).

---

**Change 6 — Map themes**

*(Deferred — implement after RPG layers are working.)*

---

**Implementation checklist**

Before writing the RPG style methods, run DescribeFeatureType on:

- `IGNF_RPG_PARCELLES-AGRICOLES-CATEGORISEES_2024:parcelles_agricole_categorisees_2024` → confirm PAC category field name and exact value strings.
- `IGNF_RPG_PARCELLES-ELIGIBLES-IAE:parcelles_eligibles_iae_2024` → confirm IAE element type field name and values.
- `IGNF_RPG_ZONES-DENSITE-HOMOGENE_2024:surfaces_2024_zdh_20250621` → confirm field name and whether numeric or categorical.

Confirmed facts (do not re-verify):
- `code_cultu` is the correct field name for `RPG.LATEST:parcelles_graphiques`.
- BBOX filtering (`minX,minY,maxX,maxY,EPSG:2154`) works identically for RPG WFS as for BDTOPO layers — same endpoint, same protocol.
- `RPG.LATEST:parcelles_graphiques` and `RPG.LATEST:ilots_anonymes` have stable LATEST aliases.
- PAC, PP, IAE, ZDH are pinned to 2024 typenames and will need updating for the 2025 edition.
