# -*- coding: utf-8 -*-
"""
sirene_display.py — Déplacement visuel des points SIRENE autour du centroïde bâtiment

Expose :
    RADIUS_MM           float  — rayon écran minimum du cercle (mm)
    RADIUS_PER_POINT_MM float  — mm supplémentaires par point dans un groupe (≥8 pts)
    build_displaced_sirene_layer(sirene_layer, buildings_layer, feedback)
        -> QgsVectorLayer

Logique :
  1. Chaque point SIRENE est apparié au centroïde du bâtiment qui le contient
     (ou le plus proche ≤ 30 m). Sans bâtiment proche : position d'origine conservée.
  2. Déduplication : un seul point par catégorie NAF par centroïde.
     Un bâtiment avec 10 commerces et 3 restaurants → 2 points, pas 13.
  3. Chaque point reçoit des champs offset_x_mm / offset_y_mm calculés en mm écran
     (RADIUS_MM = 4 mm, constant quelle que soit l'échelle de la carte).
     La géométrie reste à l'ancre ; le déplacement visuel est rendu via une
     propriété data-définie sur le symbole, appliquée par _apply_sirene_style.
  4. L'appel à _apply_sirene_style (dans fdp_par_commune.py) détecte le champ
     offset_x_mm et ajoute automatiquement le PropertyOffset data-défini.
"""

import importlib.util
import math
import os

from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsSpatialIndex,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType

# Charger SIRENE_CATEGORIES depuis sirene_buildings.py.
_sb_spec = importlib.util.spec_from_file_location(
    "sirene_buildings",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sirene_buildings.py"),
)
_sb_mod = importlib.util.module_from_spec(_sb_spec)
_sb_spec.loader.exec_module(_sb_mod)
SIRENE_CATEGORIES = _sb_mod.SIRENE_CATEGORIES
_category_index   = _sb_mod._category_index
del _sb_spec, _sb_mod

# =============================================================================
# Constantes publiques
# =============================================================================

RADIUS_M           = 8.0   # rayon minimum en mètres EPSG:2154 — 4 mm à 1:2000
RADIUS_PER_POINT_M = 1.5   # mètres supplémentaires par point dans un groupe

# =============================================================================
# Fonction publique
# =============================================================================


def build_displaced_sirene_layer(
    sirene_layer: QgsVectorLayer,
    buildings_layer: QgsVectorLayer,
    feedback,
) -> QgsVectorLayer:
    """
    Retourne une couche Point mémoire avec :
      - géométrie = centroïde du bâtiment apparié (ou coords SIRENE d'origine)
      - champs offset_x_mm / offset_y_mm encodant le déplacement circulaire
      - champ category_index pour le tri légende

    Le renderer est appliqué par _apply_sirene_style (fdp_par_commune.py) qui
    détecte offset_x_mm et ajoute automatiquement le PropertyOffset data-défini.
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
    # Clé = (anchor_key, category_index). 2 décimales suffisent en EPSG:2154 (cm).
    seen    = {}   # (anchor_key, cat_idx) → (QgsPointXY anchor, attributes)
    n_input = 0

    for src_feat in sirene_layer.getFeatures():
        n_input += 1
        naf     = src_feat["activitePrincipaleEtablissement"] or ""
        cat_idx = _category_index(naf)
        anchor  = point_to_anchor[src_feat.id()]
        key     = (f"{anchor.x():.2f},{anchor.y():.2f}", cat_idx)
        if key not in seen:
            seen[key] = (anchor, src_feat.attributes())

    feedback.pushInfo(
        f"  {n_input} établissement(s) → {len(seen)} point(s) après déduplication."
    )

    # ── Étape 4 : grouper par ancre ───────────────────────────────────────────
    anchor_groups = {}
    for (anchor_key, cat_idx), (anchor, attrs) in seen.items():
        anchor_groups.setdefault(anchor_key, []).append((cat_idx, anchor, attrs))

    for key in anchor_groups:
        anchor_groups[key].sort(key=lambda t: t[0])

    # ── Étape 5 : calculer les offsets en mètres EPSG:2154 ────────────────────
    # Géométrie = ancre ; déplacement = offset_x_m / offset_y_m en mètres CRS.
    # Rayon en mètres → le cercle grandit naturellement quand on zoome (4 mm à
    # 1:2000, 16 mm à 1:500), ce qui épouse l'échelle des emprises bâties.
    displaced = []   # (anchor QgsPointXY, attrs list, ox_m, oy_m, cat_idx)

    for entries in anchor_groups.values():
        n      = len(entries)
        anchor = entries[0][1]
        radius = max(RADIUS_M, n * RADIUS_PER_POINT_M)

        for i, (cat_idx, _anchor, attrs) in enumerate(entries):
            if n == 1:
                ox, oy = 0.0, 0.0
            else:
                angle = math.pi / 2 - (2 * math.pi * i / n)
                ox = radius * math.cos(angle)
                oy = radius * math.sin(angle)
            displaced.append((anchor, attrs, ox, oy, cat_idx))

    # ── Étape 6 : construire la couche de sortie ───────────────────────────────
    out_layer = QgsVectorLayer(
        f"Point?crs={crs_id}",
        "Établissements SIRENE",
        "memory",
    )
    pr = out_layer.dataProvider()
    base_fields = sirene_layer.fields().toList()
    pr.addAttributes(base_fields + [
        QgsField("offset_x_m",     QMetaType.Type.Double),
        QgsField("offset_y_m",     QMetaType.Type.Double),
        QgsField("category_index", QMetaType.Type.Int),
    ])
    out_layer.updateFields()

    new_features = []
    n_base = len(base_fields)
    for anchor, attrs, ox, oy, cat_idx in displaced:
        new_feat = QgsFeature(out_layer.fields())
        new_feat.setGeometry(QgsGeometry.fromPointXY(anchor))
        # attrs may be shorter than n_base if the source layer had extra fields
        padded = list(attrs) + [None] * max(0, n_base - len(attrs))
        new_feat.setAttributes(padded + [ox, oy, cat_idx])
        new_features.append(new_feat)

    pr.addFeatures(new_features)
    out_layer.updateExtents()

    feedback.pushInfo(f"  {len(new_features)} point(s) SIRENE placés.")
    return out_layer
