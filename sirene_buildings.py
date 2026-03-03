# -*- coding: utf-8 -*-
"""
sirene_buildings.py — Appariement bâtiments × établissements SIRENE

Expose :
    SIRENE_CATEGORIES   list[dict]  — 13 catégories NAF + couleurs
    EDUCATION_GRADIENT  list[tuple] — 7 sous-catégories Éducation avec codes NAF
    build_activity_layers(buildings_layer, sirene_layer, feedback)
        -> list[QgsVectorLayer]   (une couche par catégorie/sous-catégorie peuplée)
"""

import importlib.util
import os

# generate_gradient est défini dans zone_buildings.py (même dossier).
# Les scripts QGIS Processing n'ont pas de __package__, donc on utilise
# importlib avec le chemin absolu — même pattern que fdp_par_commune.py.
_zb_spec = importlib.util.spec_from_file_location(
    "zone_buildings",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "zone_buildings.py"),
)
_zb_mod = importlib.util.module_from_spec(_zb_spec)
_zb_spec.loader.exec_module(_zb_mod)
generate_gradient = _zb_mod.generate_gradient
del _zb_spec, _zb_mod

from qgis.core import (
    QgsFeatureRequest,
    QgsFillSymbol,
    QgsSingleSymbolRenderer,
    QgsSpatialIndex,
    QgsVectorLayer,
)

# =============================================================================
# Constantes publiques
# =============================================================================

# Doit rester synchronisé avec _apply_sirene_style dans fdp_par_commune.py.
# La dernière entrée est le catch-all : naf_ranges vide et pas de naf_exact_codes
# → reçoit tout ce qui n'est capturé par aucune catégorie précédente.
#
# Champs optionnels :
#   naf_exclude_suffixes  list[str]  suffixes à exclure du match de plages
#                                    (ex. Éducation exclut les codes Formation)
#   naf_exact_codes       list[str]  codes exacts à matcher avant les plages
#                                    (ex. Formation correspond à des 85.xx précis)
SIRENE_CATEGORIES = [
    {"label": "Commerce",                              "color": "#F4A261", "naf_ranges": [(45, 47)]},
    {"label": "Restauration & hébergement",            "color": "#E63946", "naf_ranges": [(55, 56)]},
    {"label": "Santé & action sociale",                "color": "#06D6A0", "naf_ranges": [(86, 88)]},
    {
        "label": "Éducation",
        "color": "#FFD166",
        "naf_ranges": [(85, 85)],
        # Exclure les codes Formation qui seraient sinon capturés par div=85
        "naf_exclude_suffixes": ["51Z", "52Z", "53Z", "59A", "59B", "60Z"],
    },
    {
        "label": "Formation",
        "color": "#B8A000",
        "naf_ranges": [],
        # Activités de formation continue, artistique, etc. dans la division 85
        "naf_exact_codes": [
            "85.51Z", "85.52Z", "85.53Z",
            "85.59A", "85.59B", "85.60Z",
        ],
    },
    {"label": "Équipements & services publics",        "color": "#C1121F", "naf_ranges": [(84, 84)]},
    {"label": "Culture, sport & loisirs",              "color": "#118AB2", "naf_ranges": [(90, 93)]},
    {"label": "Services aux personnes & associations", "color": "#F48FB1", "naf_ranges": [(94, 96)]},
    {"label": "Bureaux & services tertiaires",         "color": "#7B2D8B", "naf_ranges": [(58, 66), (68, 75), (77, 82)]},
    {"label": "Industrie, artisanat & construction",   "color": "#8B5E3C", "naf_ranges": [(5, 9), (10, 43)]},
    {"label": "Transport & logistique",                "color": "#6C757D", "naf_ranges": [(49, 53)]},
    {"label": "Agriculture, sylviculture & pêche",     "color": "#2D6A4F", "naf_ranges": [(1, 3)]},
    {"label": "Activité non classée",                  "color": "#BBBBBB", "naf_ranges": []},
]

_CATCHALL_IDX   = len(SIRENE_CATEGORIES) - 1
_EDUCATION_IDX  = next(i for i, c in enumerate(SIRENE_CATEGORIES) if c["label"] == "Éducation")

# Sous-catégories Éducation avec leurs codes NAF, du moins intense au plus intense.
# Index 0 = le plus clair (préprimaire), index -1 = le plus sombre (supérieur).
EDUCATION_GRADIENT = [
    ("85.10Z", "Enseignement préprimaire"),
    ("85.20Z", "Enseignement primaire"),
    ("85.31G", "Collège"),
    ("85.31Z", "Lycée général"),
    ("85.32Z", "Lycée professionnel"),
    ("85.41Z", "Post-bac non supérieur"),
    ("85.42Z", "Enseignement supérieur"),
]

# =============================================================================
# Helpers privés
# =============================================================================


def _naf_division(naf_code: str):
    """
    Extrait la division NAF (2 premiers caractères du code SIRENE).
    Ex. : "47.11Z" → 47.  Retourne None si le code est absent ou malformé.
    """
    if not naf_code or len(naf_code) < 2:
        return None
    try:
        return int(naf_code[:2])
    except ValueError:
        return None


def _category_index(naf_code: str) -> int:
    """
    Retourne l'index dans SIRENE_CATEGORIES pour le code NAF complet.

    Deux passes :
      1. Codes exacts (naf_exact_codes) — capturent la Formation avant Éducation.
      2. Plages de divisions (naf_ranges) avec exclusions (naf_exclude_suffixes).

    Si aucune correspondance, retourne _CATCHALL_IDX.
    """
    naf_clean = (naf_code or "").strip()
    naf_div   = _naf_division(naf_clean)

    # Passe 1 — codes exacts (ex. Formation)
    for idx, cat in enumerate(SIRENE_CATEGORIES[:-1]):
        exact = cat.get("naf_exact_codes")
        if exact and naf_clean in exact:
            return idx

    # Passe 2 — plages de divisions avec exclusions optionnelles (ex. Éducation)
    if naf_div is not None:
        for idx, cat in enumerate(SIRENE_CATEGORIES[:-1]):
            for lo, hi in cat.get("naf_ranges", []):
                if lo <= naf_div <= hi:
                    excludes = cat.get("naf_exclude_suffixes", ())
                    if any(naf_clean.endswith(suf) for suf in excludes):
                        break   # exclu de cette catégorie, passer à la suivante
                    return idx

    return _CATCHALL_IDX


# =============================================================================
# Fonction publique
# =============================================================================


def build_activity_layers(
    buildings_layer: QgsVectorLayer,
    sirene_layer: QgsVectorLayer,
    feedback,
) -> list:
    """
    Apparie chaque point SIRENE au bâtiment qui le contient, avec repli sur
    le bâtiment le plus proche dans un rayon de 10 m si aucun containment
    n'est trouvé. Génère ensuite des couches mémoire colorées par catégorie NAF.

    Cas particulier Éducation : génère jusqu'à 8 sous-couches en dégradé
    (une par code EDUCATION_GRADIENT + une catch-all) plutôt qu'une couche plate.

    Retourne les couches dans l'ordre SIRENE_CATEGORIES, puis par intensité
    au sein d'Éducation (plus clair en premier).
    """
    crs_id   = buildings_layer.crs().authid()
    BUFFER_M = 10.0

    # ── Étape 1 : catégoriser les points SIRENE ────────────────────────────────
    sirene_cat  = {}   # fid → category index
    sirene_naf  = {}   # fid → full NAF code (needed for Éducation gradient)
    sirene_geom = {}   # fid → QgsGeometry (déjà en EPSG:2154)
    for feat in sirene_layer.getFeatures():
        naf = feat["activitePrincipaleEtablissement"] or ""
        sirene_naf[feat.id()]  = naf
        sirene_cat[feat.id()]  = _category_index(naf)
        sirene_geom[feat.id()] = feat.geometry()

    if not sirene_cat:
        return []

    # ── Étape 2 : index spatial + cache géométrie des bâtiments ───────────────
    bld_index = QgsSpatialIndex(buildings_layer.getFeatures())
    bld_geom  = {}
    for feat in buildings_layer.getFeatures():
        bld_geom[feat.id()] = feat.geometry()

    if not bld_geom:
        return []

    # ── Étape 3 : appariement SIRENE → bâtiments ──────────────────────────────
    # building_to_cats  : bld_fid → set(cat_idx)
    # edu_bld_to_naf    : bld_fid → set(naf_code)  — Éducation uniquement
    building_to_cats = {}
    edu_bld_to_naf   = {}

    for processed, (s_fid, s_geom) in enumerate(sirene_geom.items()):
        if processed % 500 == 0 and feedback.isCanceled():
            return []

        bbox = s_geom.boundingBox()
        bbox.grow(BUFFER_M)
        candidates = bld_index.intersects(bbox)
        if not candidates:
            continue

        cat_idx = sirene_cat[s_fid]

        # Test 1 — containment strict : le polygone bâtiment contient le point
        matched_fid = None
        for bld_fid in candidates:
            if bld_geom[bld_fid].contains(s_geom):
                matched_fid = bld_fid
                break

        # Test 2 — fallback distance : bâtiment le plus proche dans le rayon
        if matched_fid is None:
            min_dist = BUFFER_M + 1.0
            for bld_fid in candidates:
                d = s_geom.distance(bld_geom[bld_fid])
                if d <= BUFFER_M and d < min_dist:
                    min_dist    = d
                    matched_fid = bld_fid

        if matched_fid is not None:
            building_to_cats.setdefault(matched_fid, set()).add(cat_idx)
            if cat_idx == _EDUCATION_IDX:
                edu_bld_to_naf.setdefault(matched_fid, set()).add(sirene_naf[s_fid])

    if not building_to_cats:
        return []

    # ── Étape 4 : une couche mémoire par catégorie avec ≥ 1 bâtiment ──────────
    fields  = buildings_layer.fields()
    results = []

    # Pré-calculer les outils du dégradé Éducation une seule fois
    _edu_colors      = generate_gradient("#FFD166", len(EDUCATION_GRADIENT))
    _edu_naf_to_step = {code: i for i, (code, _) in enumerate(EDUCATION_GRADIENT)}

    for cat_idx, cat in enumerate(SIRENE_CATEGORIES):
        matched_fids = [
            fid for fid, cats in building_to_cats.items() if cat_idx in cats
        ]
        if not matched_fids:
            continue

        # ── Éducation : sous-couches en dégradé par code NAF ──────────────
        if cat_idx == _EDUCATION_IDX:
            # Pour chaque fid Education, mapper ses codes NAF aux slots du dégradé
            edu_naf_to_fids = {}
            for fid in matched_fids:
                for naf in edu_bld_to_naf.get(fid, set()):
                    if naf in _edu_naf_to_step:
                        edu_naf_to_fids.setdefault(naf, []).append(fid)

            # Sous-couches dans l'ordre EDUCATION_GRADIENT (lightest → darkest)
            for step_idx, (naf_code, label) in enumerate(EDUCATION_GRADIENT):
                fids = edu_naf_to_fids.get(naf_code)
                if not fids:
                    continue
                color = _edu_colors[step_idx]
                mem_layer = QgsVectorLayer(
                    f"Polygon?crs={crs_id}",
                    f"Bâti — Éducation — {label}",
                    "memory",
                )
                pr = mem_layer.dataProvider()
                pr.addAttributes(fields.toList())
                mem_layer.updateFields()
                pr.addFeatures(list(buildings_layer.getFeatures(
                    QgsFeatureRequest().setFilterFids(fids)
                )))
                mem_layer.updateExtents()
                c         = color
                color_str = f"{c.red()},{c.green()},{c.blue()},{c.alpha()}"
                sym = QgsFillSymbol.createSimple({
                    "color":         color_str,
                    "outline_style": "no",
                })
                mem_layer.setRenderer(QgsSingleSymbolRenderer(sym))
                results.append(mem_layer)

            # Catch-all : bâtiments Education sans code gradient reconnu
            edu_catchall = [
                fid for fid in matched_fids
                if not any(naf in _edu_naf_to_step
                           for naf in edu_bld_to_naf.get(fid, set()))
            ]
            if edu_catchall:
                mem_layer = QgsVectorLayer(
                    f"Polygon?crs={crs_id}",
                    "Bâti — Éducation — Autre",
                    "memory",
                )
                pr = mem_layer.dataProvider()
                pr.addAttributes(fields.toList())
                mem_layer.updateFields()
                pr.addFeatures(list(buildings_layer.getFeatures(
                    QgsFeatureRequest().setFilterFids(edu_catchall)
                )))
                mem_layer.updateExtents()
                sym = QgsFillSymbol.createSimple({
                    "color":         "#FFD166",
                    "outline_style": "no",
                })
                mem_layer.setRenderer(QgsSingleSymbolRenderer(sym))
                results.append(mem_layer)

            continue   # Éducation traitée — passer à la catégorie suivante

        # ── Autres catégories : une couche plate ───────────────────────────
        mem_layer = QgsVectorLayer(
            f"Polygon?crs={crs_id}",
            f"Bâti — {cat['label']}",
            "memory",
        )
        pr = mem_layer.dataProvider()
        pr.addAttributes(fields.toList())
        mem_layer.updateFields()

        pr.addFeatures(list(buildings_layer.getFeatures(
            QgsFeatureRequest().setFilterFids(matched_fids)
        )))
        mem_layer.updateExtents()

        sym = QgsFillSymbol.createSimple({
            "color":         cat["color"],
            "outline_style": "no",
        })
        mem_layer.setRenderer(QgsSingleSymbolRenderer(sym))
        results.append(mem_layer)

    return results
