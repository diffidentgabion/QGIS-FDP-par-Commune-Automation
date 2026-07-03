Here it is — paste this in full:

---

**Context:** We are adding zone d'activité ou d'intérêt (ZAI) building classification to the existing QGIS basemap script. The working codebase consists of `fdp_par_commune.py` (main script) and `sirene_buildings.py` (SIRENE building classification). We are adding a new `zone_buildings.py` file and making targeted changes to the two existing files. Do not rewrite anything that already works.

---

**Change 1 — ZAI layer in default layer stack (`fdp_par_commune.py`)**

In `wfs_definitions`, insert the following entry immediately above the `CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle` entry:

```python
(
    "BDTOPO_V3:zone_d_activite_ou_d_interet",
    "Zones d'activité et d'intérêt",
    "zai",
),
```

After loading and clipping, store this layer separately in a dedicated variable `zai_layer` in addition to `loaded_layers` — it needs to be passed to `build_zone_activity_layers` later. It should still appear in the main layer group at its correct stack position.

Add `_apply_zai_style(layer)` as a new method on `FDPParCommune`. Call it from `_apply_style` when `style_key == "zai"`. The method applies a `QgsRuleBasedRenderer` with one rule per ZAI `categorie` value, using solid fills at full opacity with the lightened category colors below. Each rule's filter expression is `"categorie" = 'VALUE'`. Add an ELSE fallback rule with fill `#E8E8E8`.

Before writing the rule expressions, confirm whether the Géoplateforme WFS returns the `categorie` attribute with full French accents as documented (`"Santé"`, `"Culture et loisirs"`, etc.) or stripped. State your assumption clearly in a comment in the code, and make the filter expressions match accordingly.

The 8 rules and their lightened fill colors:

| Catégorie | Zone fill |
|---|---|
| Science et enseignement | `#FFF0B3` |
| Santé | `#B3F5E6` |
| Administratif ou militaire | `#F5B3B6` |
| Industriel et commercial | `#E8D5C4` |
| Culture et loisirs | `#B3DFF0` |
| Sport | `#FCDEC4` |
| Religieux | `#E0D0E8` |
| Gestion des eaux | `#C4E3F5` |

Also add filtering of fictif ZAI features. After loading and clipping the ZAI layer, run a second pass that removes any feature where the `fictif` attribute equals `"Vrai"` or `True` (check both since WFS type handling varies). Do this in-place on the memory layer using `layer.dataProvider().deleteFeatures(ids_to_delete)`.

---

**Change 2 — New file `zone_buildings.py`**

Create `zone_buildings.py` in the same folder. It contains:

**Module-level constants:**

```python
from qgis.core import QgsVectorLayer, QgsFeature, QgsSpatialIndex, QgsFields
from qgis.core import QgsFillSymbol, QgsSingleSymbolRenderer, QgsRuleBasedRenderer
from qgis.PyQt.QtGui import QColor

ZAI_CATEGORIES = [
    {
        "categorie": "Science et enseignement",
        "label": "Éducation",
        "base_color": "#FFD166",
        "zone_color": "#FFF0B3",
        "natures_ordered": [
            "Etablissement d'enseignement primaire",
            "Etablissement d'enseignement secondaire",
            "Etablissement d'enseignement supérieur",
        ],
        "catch_all_label": "Autre enseignement ou recherche",
    },
    {
        "categorie": "Santé",
        "label": "Santé",
        "base_color": "#06D6A0",
        "zone_color": "#B3F5E6",
        "natures_ordered": [
            "Etablissement thermal",
            "Etablissement de soins",
            "Hôpital",
        ],
        "catch_all_label": "Autre santé",
    },
    {
        "categorie": "Administratif ou militaire",
        "label": "Administratif & militaire",
        "base_color": "#C1121F",
        "zone_color": "#F5B3B6",
        "natures_ordered": [
            "Divers public ou administratif",
            "Caserne de pompiers",
            "Caserne",
            "Enceinte militaire",
            "Administration centrale de l'Etat",
        ],
        "catch_all_label": "Autre administratif",
    },
    {
        "categorie": "Industriel et commercial",
        "label": "Industriel & commercial",
        "base_color": "#8B5E3C",
        "zone_color": "#E8D5C4",
        "natures_ordered": [
            "Divers commercial",
            "Marché",
            "Divers industriel",
            "Usine",
            "Zone industrielle",
        ],
        "catch_all_label": "Autre industriel",
    },
    {
        "categorie": "Culture et loisirs",
        "label": "Culture & loisirs",
        "base_color": "#118AB2",
        "zone_color": "#B3DFF0",
        "natures_ordered": [
            "Musée",
            "Cinéma",
            "Théâtre",
            "Salle de spectacle",
        ],
        "catch_all_label": "Autre culture et loisirs",
    },
    {
        "categorie": "Sport",
        "label": "Sport",
        "base_color": "#F4A261",
        "zone_color": "#FCDEC4",
        "natures_ordered": [],
        "catch_all_label": "Équipement sportif",
    },
    {
        "categorie": "Religieux",
        "label": "Religieux",
        "base_color": "#9B72AA",
        "zone_color": "#E0D0E8",
        "natures_ordered": [],
        "catch_all_label": "Lieu de culte",
    },
    {
        "categorie": "Gestion des eaux",
        "label": "Gestion des eaux",
        "base_color": "#6baed6",
        "zone_color": "#C4E3F5",
        "natures_ordered": [
            "Station de pompage",
            "Station d'épuration",
            "Usine de production d'eau potable",
        ],
        "catch_all_label": "Autre gestion des eaux",
    },
]
```

**Shared gradient utility — define here, import from here:**

```python
def generate_gradient(base_color_hex: str, n_steps: int) -> list:
    """
    Returns n_steps QColor objects darkening from base_color toward ~45% HSV value.
    Index 0 = lightest (least intense), index -1 = darkest (most intense).
    If n_steps == 1, returns [QColor(base_color_hex)].
    """
```

Implement using `QColor.fromHsvF(h, s, v)` where `v` steps from `1.0` down to `0.45` in `n_steps` equal increments. Hue and saturation taken from the base color unchanged.

**Public function:**

```python
def build_zone_activity_layers(
    buildings_layer: QgsVectorLayer,
    zai_layer: QgsVectorLayer,
    feedback,
) -> list[QgsVectorLayer]:
```

**Step-by-step logic:**

Step 1 — Build a `QgsSpatialIndex` on `buildings_layer`. Build a dict `{fid: QgsGeometry}` for all building features.

Step 2 — Iterate ZAI features. For each feature, read `categorie` and `nature` attributes (normalize to stripped strings, handle None). Skip features where `fictif` is `"Vrai"` (belt-and-suspenders check in case the earlier deletion pass missed any). For each ZAI polygon, query the spatial index for candidate buildings whose bbox intersects the ZAI bbox, then test `zai_geom.intersects(building_geom)` for each candidate. Record matches as `building_matches: dict[int, list[tuple[str, str]]]` mapping `building_fid → [(categorie, nature), ...]`. A building can accumulate multiple entries if it intersects multiple ZAI zones. Check `feedback.isCanceled()` every 200 features.

Step 3 — For buildings intersecting multiple ZAI zones of different categories, include that building in every matching category's layers — do not deduplicate across categories.

Step 4 — For each entry in `ZAI_CATEGORIES`, collect all matched buildings grouped by `nature`. Determine nature ordering: natures in `natures_ordered` get gradient positions 0 through len-1 in that order (lightest to darkest). Natures not in `natures_ordered` are grouped together under `catch_all_label` and assigned the base color. Generate gradient colors using `generate_gradient(base_color, n_steps)` where `n_steps = len(natures_ordered)`. If `natures_ordered` is empty (Sport, Religieux), all natures collapse into one layer using the base color.

Step 5 — For each nature group with at least one building, create a memory `Polygon` layer with the same CRS and fields as `buildings_layer`. Copy full geometry and all attributes for each matched building feature. Set layer name: `f"Zones — {category['label']} — {nature_label}"`. Apply `QgsSingleSymbolRenderer` with `QgsFillSymbol` using the computed gradient color, no outline. Append to results.

Step 6 — Return results ordered by `ZAI_CATEGORIES` index, then by nature intensity within each category (lightest first, so darkest sits highest in the QGIS legend).

---

**Change 3 — Éducation/Formation split and gradient in `sirene_buildings.py`**

Import `generate_gradient` from `zone_buildings.py`:
```python
from zone_buildings import generate_gradient
```
(Use `importlib` with absolute path if relative import fails in QGIS processing context — same pattern already established in the project.)

In `SIRENE_CATEGORIES`, replace the existing `"Enseignement"` entry with two entries:

```python
{
    "label": "Éducation",
    "color": "#FFD166",
    "naf_ranges": [(85, 85)],
    "naf_exclude_suffixes": ["51Z", "52Z", "53Z", "59A", "59B", "60Z"],
},
{
    "label": "Formation",
    "color": "#B8A000",
    "naf_ranges": [],
    "naf_exact_codes": [
        "85.51Z", "85.52Z", "85.53Z",
        "85.59A", "85.59B", "85.60Z"
    ],
},
```

Update the category matching logic in `build_activity_layers` to handle `naf_exclude_suffixes` (exclude from range match if the full NAF code ends with one of these) and `naf_exact_codes` (match only these exact codes regardless of ranges).

Update `_naf_div_expr` in `fdp_par_commune.py` for the SIRENE point rule-based renderer to match — the Éducation rule should exclude the Formation codes using a NOT IN expression on the full `activitePrincipaleEtablissement` field.

**Éducation gradient sublayers:**

For the Éducation category only, instead of generating one flat-colored building layer, generate up to 7 sublayers one per NAF code in intensity order:

```python
EDUCATION_GRADIENT = [
    ("85.10Z", "Enseignement préprimaire"),      # lightest
    ("85.20Z", "Enseignement primaire"),
    ("85.31G", "Collège"),
    ("85.31Z", "Lycée général"),
    ("85.32Z", "Lycée professionnel"),
    ("85.41Z", "Post-bac non supérieur"),
    ("85.42Z", "Enseignement supérieur"),         # darkest
]
```

Use `generate_gradient("#FFD166", 7)` to get the 7 shades. Buildings whose NAF code doesn't match any of these exactly go into a catch-all `"Éducation — Autre"` layer using the base color `#FFD166`. Layer names: `f"Bâti — Éducation — {label}"`.

---

**Integration in `processAlgorithm` (`fdp_par_commune.py`)**

Add import at top of file:
```python
from zone_buildings import build_zone_activity_layers
```

After the existing SIRENE building layers block and before group construction, add:

```python
if zai_layer and "buildings" in loaded_layers:
    feedback.pushInfo("Génération des bâtiments par zone d'activité…")
    zone_layers = build_zone_activity_layers(
        loaded_layers["buildings"],
        zai_layer,
        feedback,
    )
    feedback.pushInfo(f"{len(zone_layers)} couche(s) de zones générée(s).")
else:
    zone_layers = []
```

**Updated layer group structure:**

```
[Commune name]
├── Établissements SIRENE
├── Bâti par activité SIRENE
│   ├── Bâti — Commerce
│   ├── Bâti — Éducation — Enseignement supérieur   ← darkest
│   ├── Bâti — Éducation — Lycée général
│   ├── … etc lightest last
│   ├── Bâti — Éducation — Autre
│   └── Bâti — Formation
├── Bâti par zone d'activité                         ← new separate subgroup
│   ├── Zones — Santé — Hôpital                     ← darkest first
│   ├── Zones — Santé — Etablissement de soins
│   ├── Zones — Éducation — Enseignement supérieur
│   └── … etc
├── Zones d'activité et d'intérêt                    ← base ZAI polygon layer
├── Bâti                                             ← unchanged grey base
├── Voirie
└── …
```

A building intersecting multiple ZAI zones of the same category but different natures should appear in all matching nature sublayers. Exhaustivity is required for thematic exports where a single category is viewed in isolation.

**Before writing any code**, answer the following:

1. The Géoplateforme WFS returns feature attributes with their original casing and accents as documented — confirm this is your assumption for the `categorie` field filter expressions, and state what fallback you will use if a feature's `categorie` value doesn't match any of the 8 known values.

2. `generate_gradient` will be imported by both `zone_buildings.py` (defined there) and `sirene_buildings.py` (imported from there). In a QGIS processing script context where relative imports may not work, show the exact import statement you will use in `sirene_buildings.py` to import from `zone_buildings.py` in the same folder.
