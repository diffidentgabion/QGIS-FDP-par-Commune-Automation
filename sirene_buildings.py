# -*- coding: utf-8 -*-
"""
sirene_buildings.py — Appariement bâtiments × établissements SIRENE

Expose :
    SIRENE_CATEGORIES  list[dict]  — 12 catégories NAF + couleurs
    build_activity_layers(buildings_layer, sirene_layer, feedback)
        -> list[QgsVectorLayer]   (une couche par catégorie ayant ≥ 1 bâtiment)
"""

from qgis.core import (
    QgsFeatureRequest,
    QgsFillSymbol,
    QgsSingleSymbolRenderer,
    QgsSpatialIndex,
    QgsVectorLayer,
)

# Catégories NAF et leurs couleurs.
# Doit rester synchronisé avec _apply_sirene_style dans fdp_par_commune.py.
# La dernière entrée est le catch-all : naf_ranges vide → reçoit tout ce qui
# n'a pas été capturé par les plages des catégories précédentes.
SIRENE_CATEGORIES = [
    {"label": "Commerce",                              "color": "#F4A261", "naf_ranges": [(45, 47)]},
    {"label": "Restauration & hébergement",            "color": "#E63946", "naf_ranges": [(55, 56)]},
    {"label": "Santé & action sociale",                "color": "#06D6A0", "naf_ranges": [(86, 88)]},
    {"label": "Enseignement",                          "color": "#FFD166", "naf_ranges": [(85, 85)]},
    {"label": "Équipements & services publics",        "color": "#C1121F", "naf_ranges": [(84, 84)]},
    {"label": "Culture, sport & loisirs",              "color": "#118AB2", "naf_ranges": [(90, 93)]},
    {"label": "Services aux personnes & associations", "color": "#F48FB1", "naf_ranges": [(94, 96)]},
    {"label": "Bureaux & services tertiaires",         "color": "#7B2D8B", "naf_ranges": [(58, 66), (68, 75), (77, 82)]},
    {"label": "Industrie, artisanat & construction",   "color": "#8B5E3C", "naf_ranges": [(5, 9), (10, 43)]},
    {"label": "Transport & logistique",                "color": "#6C757D", "naf_ranges": [(49, 53)]},
    {"label": "Agriculture, sylviculture & pêche",     "color": "#2D6A4F", "naf_ranges": [(1, 3)]},
    {"label": "Activité non classée",                  "color": "#BBBBBB", "naf_ranges": []},
]

_CATCHALL_IDX = len(SIRENE_CATEGORIES) - 1  # index de "Activité non classée"


# =============================================================================
# Helpers privés
# =============================================================================

def _naf_division(naf_code: str):
    """
    Extrait la division NAF des 2 premiers caractères du code SIRENE.
    Ex. : "47.11Z" → 47.  Retourne None si le code est absent ou malformé.
    """
    if not naf_code or len(naf_code) < 2:
        return None
    try:
        return int(naf_code[:2])
    except ValueError:
        return None


def _category_index(naf_div) -> int:
    """
    Retourne l'index dans SIRENE_CATEGORIES correspondant à la division NAF.
    Si aucune plage ne correspond (ou naf_div est None), retourne _CATCHALL_IDX.
    """
    if naf_div is not None:
        for idx, cat in enumerate(SIRENE_CATEGORIES[:-1]):  # skip catch-all
            for lo, hi in cat["naf_ranges"]:
                if lo <= naf_div <= hi:
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
    n'est trouvé. Génère ensuite une couche mémoire colorée par catégorie NAF
    pour chaque catégorie ayant au moins un bâtiment apparié.

    Performance :
      - QgsSpatialIndex fonctionne correctement sur les couches mémoire
        (provider "memory") : il itère via QgsFeatureIterator, indépendant
        du type de fournisseur.
      - Pour les grandes communes (50 000+ bâtiments), l'index spatial
        maintient la complexité en O(n log n).
      - feedback.isCanceled() est testé toutes les 500 entités SIRENE
        pour permettre l'annulation sans bloquer QGIS.

    Retourne les couches dans l'ordre de SIRENE_CATEGORIES.
    """
    crs_id   = buildings_layer.crs().authid()
    BUFFER_M = 10.0

    # ── Étape 1 : catégoriser les points SIRENE ────────────────────────────────
    sirene_cat  = {}   # fid → category index
    sirene_geom = {}   # fid → QgsGeometry (déjà en EPSG:2154)
    for feat in sirene_layer.getFeatures():
        naf = feat["activitePrincipaleEtablissement"] or ""
        sirene_cat[feat.id()]  = _category_index(_naf_division(naf))
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
    # building_to_cats : bld_fid → set de category indices
    # Un même bâtiment peut appartenir à plusieurs catégories (ex. commerce +
    # restauration dans le même immeuble).
    building_to_cats = {}

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
                    min_dist  = d
                    matched_fid = bld_fid

        if matched_fid is not None:
            building_to_cats.setdefault(matched_fid, set()).add(cat_idx)

    if not building_to_cats:
        return []

    # ── Étape 4 : une couche mémoire par catégorie avec ≥ 1 bâtiment ──────────
    fields  = buildings_layer.fields()
    results = []

    for cat_idx, cat in enumerate(SIRENE_CATEGORIES):
        matched_fids = [
            fid for fid, cats in building_to_cats.items() if cat_idx in cats
        ]
        if not matched_fids:
            continue

        mem_layer = QgsVectorLayer(
            f"Polygon?crs={crs_id}",
            f"Bâti — {cat['label']}",
            "memory",
        )
        pr = mem_layer.dataProvider()
        pr.addAttributes(fields.toList())
        mem_layer.updateFields()

        # QgsFeatureRequest.setFilterFids() est efficace sur les couches mémoire
        # (le provider indexe les entités par fid en interne).
        feats = list(buildings_layer.getFeatures(
            QgsFeatureRequest().setFilterFids(matched_fids)
        ))
        pr.addFeatures(feats)
        mem_layer.updateExtents()

        sym = QgsFillSymbol.createSimple({
            "color":         cat["color"],
            "outline_style": "no",
        })
        mem_layer.setRenderer(QgsSingleSymbolRenderer(sym))
        results.append(mem_layer)

    return results
