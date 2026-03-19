# -*- coding: utf-8 -*-
"""
sirene_display.py — Déplacement visuel des points SIRENE autour du centroïde bâtiment

Expose :
    RADIUS_M            float  — rayon minimum du cercle (mètres, EPSG:2154)
    RADIUS_PER_POINT_M  float  — mètres supplémentaires par point dans un groupe
    build_displaced_sirene_layer(sirene_layer, buildings_layer, feedback)
        -> QgsVectorLayer

Logique :
  1. Chaque point SIRENE est apparié au centroïde du bâtiment qui le contient
     (ou le plus proche ≤ 30 m). Sans bâtiment proche : position d'origine conservée.
  2. Déduplication : un seul point par catégorie NAF par centroïde.
     Un bâtiment avec 10 commerces et 3 restaurants → 2 points, pas 13.
  3. Les points partageant un centroïde sont répartis en cercle autour de celui-ci,
     leurs coordonnées EPSG:2154 finales calculées directement.
     Max 12-13 points par bâtiment → rayon max ~20 m, soit ≤ 10 mm à 1:2000.
  4. Renderer règle-par-règle simple — aucune dépendance aux APIs renderer avancées.
"""

import importlib.util
import math
import os

from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsMarkerSymbol,
    QgsPointXY,
    QgsRuleBasedRenderer,
    QgsSpatialIndex,
    QgsVectorLayer,
)

# Charger SIRENE_CATEGORIES depuis sirene_buildings.py.
# Les scripts Processing n'ont pas de __package__, donc on utilise importlib
# avec le chemin absolu — même pattern que fdp_par_commune.py.
_sb_spec = importlib.util.spec_from_file_location(
    "sirene_buildings",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sirene_buildings.py"),
)
_sb_mod = importlib.util.module_from_spec(_sb_spec)
_sb_spec.loader.exec_module(_sb_mod)
SIRENE_CATEGORIES = _sb_mod.SIRENE_CATEGORIES
_category_index   = _sb_mod._category_index   # évite la duplication de la logique NAF
del _sb_spec, _sb_mod

# =============================================================================
# Constantes publiques
# =============================================================================

RADIUS_M           = 8.0   # rayon minimum (mètres EPSG:2154) — 4 mm à 1:2000
RADIUS_PER_POINT_M = 1.5   # mètres supplémentaires par point dans un groupe

# =============================================================================
# Helpers privés
# =============================================================================

_CATCHALL_IDX = len(SIRENE_CATEGORIES) - 1


def _naf_div_expr(ranges: list) -> str:
    """Expression QGIS filtrant par plages de divisions NAF."""
    field   = 'to_int(left("activitePrincipaleEtablissement", 2))'
    clauses = [f"({field} BETWEEN {lo} AND {hi})" for lo, hi in ranges]
    return " OR ".join(clauses) if clauses else "FALSE"


# =============================================================================
# Fonction publique
# =============================================================================


def build_displaced_sirene_layer(
    sirene_layer: QgsVectorLayer,
    buildings_layer: QgsVectorLayer,
    feedback,
) -> QgsVectorLayer:
    """
    Retourne une couche Point mémoire avec coordonnées finales déplacées.
    Voir module docstring pour la logique complète.
    """
    crs_id   = sirene_layer.crs().authid()
    BUFFER_M = 30.0

    # ── Étape 1 : index spatial + centroïdes des bâtiments ────────────────────
    feedback.pushInfo("  Construction de l'index spatial des bâtiments…")
    bld_index     = QgsSpatialIndex(buildings_layer.getFeatures())
    bld_centroids = {}
    bld_geoms     = {}
    for feat in buildings_layer.getFeatures():
        geom = feat.geometry()
        bld_geoms[feat.id()]     = geom
        bld_centroids[feat.id()] = geom.centroid().asPoint()

    # ── Étape 2 : apparier chaque point SIRENE à un centroïde bâtiment ────────
    feedback.pushInfo("  Appariement SIRENE → bâtiments…")
    point_to_anchor = {}   # sirene fid → QgsPointXY

    for processed, feat in enumerate(sirene_layer.getFeatures()):
        if processed % 500 == 0 and feedback.isCanceled():
            return sirene_layer

        s_geom = feat.geometry()
        bbox   = s_geom.boundingBox()
        bbox.grow(BUFFER_M)
        candidates = bld_index.intersects(bbox)

        matched_centroid = None

        for bld_fid in candidates:
            if bld_geoms[bld_fid].contains(s_geom):
                matched_centroid = bld_centroids[bld_fid]
                break

        if matched_centroid is None and candidates:
            min_dist = BUFFER_M + 1.0
            for bld_fid in candidates:
                d = s_geom.distance(bld_geoms[bld_fid])
                if d <= BUFFER_M and d < min_dist:
                    min_dist         = d
                    matched_centroid = bld_centroids[bld_fid]

        if matched_centroid is None:
            matched_centroid = s_geom.asPoint()

        point_to_anchor[feat.id()] = matched_centroid

    # ── Étape 3 : déduplication (un point par catégorie par ancre) ────────────
    # Clé = (anchor_key, category_index). On garde le premier représentant trouvé.
    # Résultat : au plus len(SIRENE_CATEGORIES) ≈ 13 points par bâtiment.
    seen    = {}   # (anchor_key, cat_idx) → (QgsPointXY anchor, attributes)
    n_input = 0

    for src_feat in sirene_layer.getFeatures():
        n_input += 1
        naf     = src_feat["activitePrincipaleEtablissement"] or ""
        cat_idx = _category_index(naf)
        anchor  = point_to_anchor[src_feat.id()]
        key     = (f"{anchor.x():.3f},{anchor.y():.3f}", cat_idx)
        if key not in seen:
            seen[key] = (anchor, src_feat.attributes())

    feedback.pushInfo(
        f"  {n_input} établissement(s) → {len(seen)} point(s) après déduplication."
    )

    # ── Étape 4 : grouper par ancre et calculer les positions déplacées ────────
    # anchor_key → [(cat_idx, anchor, attributes), ...]
    anchor_groups = {}
    for (anchor_key, cat_idx), (anchor, attrs) in seen.items():
        anchor_groups.setdefault(anchor_key, []).append((cat_idx, anchor, attrs))

    # Trier par cat_idx → disposition horaire dans l'ordre de la légende
    for key in anchor_groups:
        anchor_groups[key].sort(key=lambda t: t[0])

    # Calculer la géométrie finale de chaque point
    displaced = []   # liste de (QgsGeometry, attributes)
    for entries in anchor_groups.values():
        n      = len(entries)
        anchor = entries[0][1]
        radius = max(RADIUS_M, n * RADIUS_PER_POINT_M)

        for i, (cat_idx, _anchor, attrs) in enumerate(entries):
            if n == 1:
                geom = QgsGeometry.fromPointXY(anchor)
            else:
                # Départ au nord (π/2) puis sens horaire
                angle = math.pi / 2 - (2 * math.pi * i / n)
                x = anchor.x() + radius * math.cos(angle)
                y = anchor.y() + radius * math.sin(angle)
                geom = QgsGeometry.fromPointXY(QgsPointXY(x, y))
            displaced.append((geom, attrs))

    # ── Étape 5 : construire la couche de sortie ───────────────────────────────
    out_layer = QgsVectorLayer(
        f"Point?crs={crs_id}",
        "Établissements SIRENE (déplacés)",
        "memory",
    )
    pr = out_layer.dataProvider()
    pr.addAttributes(sirene_layer.fields().toList())
    out_layer.updateFields()

    new_features = []
    for geom, attrs in displaced:
        new_feat = QgsFeature(out_layer.fields())
        new_feat.setGeometry(geom)
        new_feat.setAttributes(attrs)
        new_features.append(new_feat)

    pr.addFeatures(new_features)
    out_layer.updateExtents()

    # ── Étape 6 : renderer règle-par-règle simple ─────────────────────────────
    # Pas de propriétés data-définies ni de renderer avancé — le déplacement est
    # dans la géométrie. Doit rester synchronisé avec _apply_sirene_style dans
    # fdp_par_commune.py.

    _FORMATION_CODES = "'85.51Z','85.52Z','85.53Z','85.59A','85.59B','85.60Z'"
    _div             = 'to_int(left("activitePrincipaleEtablissement", 2))'

    groups = [
        ("Commerce",                              [(45, 47)],                     "#F4A261", 3.0, "circle",     None),
        ("Restauration & hébergement",            [(55, 56)],                     "#E63946", 3.0, "square",     None),
        ("Santé & action sociale",                [(86, 88)],                     "#06D6A0", 3.0, "diamond",    None),
        (
            "Éducation",
            [(85, 85)],
            "#FFD166",
            3.0,
            "triangle",
            f'({_div} = 85) AND "activitePrincipaleEtablissement" NOT IN ({_FORMATION_CODES})',
        ),
        (
            "Formation",
            [],
            "#B8A000",
            3.0,
            "star",
            f'"activitePrincipaleEtablissement" IN ({_FORMATION_CODES})',
        ),
        ("Équipements & services publics",        [(84, 84)],                     "#C1121F", 3.0, "pentagon",   None),
        ("Culture, sport & loisirs",              [(90, 93)],                     "#118AB2", 3.0, "hexagon",    None),
        ("Services aux personnes & associations", [(94, 96)],                     "#F48FB1", 2.5, "cross_fill", None),
        ("Bureaux & services tertiaires",         [(58, 66), (68, 75), (77, 82)], "#7B2D8B", 2.5, "circle",    None),
        ("Industrie, artisanat & construction",   [(5, 9), (10, 43)],             "#8B5E3C", 2.5, "square",    None),
        ("Transport & logistique",                [(49, 53)],                     "#6C757D", 2.5, "diamond",   None),
        ("Agriculture, sylviculture & pêche",     [(1, 3)],                       "#2D6A4F", 2.5, "triangle",  None),
    ]

    root_rule = QgsRuleBasedRenderer.Rule(None)

    for label, ranges, color, size, shape, custom_expr in groups:
        sym = QgsMarkerSymbol.createSimple({
            "color":         color,
            "name":          shape,
            "size":          str(size),
            "outline_style": "no",
        })
        rule = QgsRuleBasedRenderer.Rule(sym)
        rule.setFilterExpression(
            custom_expr if custom_expr is not None else _naf_div_expr(ranges)
        )
        rule.setLabel(label)
        root_rule.appendChild(rule)

    other_sym = QgsMarkerSymbol.createSimple({
        "color":         "#BBBBBB",
        "name":          "circle",
        "size":          "1.5",
        "outline_style": "no",
    })
    other_rule = QgsRuleBasedRenderer.Rule(other_sym)
    other_rule.setFilterExpression("ELSE")
    other_rule.setLabel("Activité non classée")
    root_rule.appendChild(other_rule)

    out_layer.setRenderer(QgsRuleBasedRenderer(root_rule))
    out_layer.triggerRepaint()

    feedback.pushInfo(f"  {len(new_features)} point(s) SIRENE chargés.")
    return out_layer
