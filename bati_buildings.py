# -*- coding: utf-8 -*-
"""
bati_buildings.py — Classification intrinsèque du bâti BDTOPO

Expose :
    build_bati_layers(buildings_layer, feedback) -> list[QgsVectorLayer]

        Classifie le bâti selon ses propres attributs (sans croisement spatial) :

          Résidentiel   → nombre_de_logements >= 1
          Religieux     → nature in {"église", "chapelle"}
          Château       → nature == "château"
          Industriel    → usage_1 == "industriel"
                          OU nature == "industriel, agricole ou commercial"
          Non classé    → tous les autres bâtiments

        Génère également deux couches statistiques graduées :
          Bâti — Densité résidentielle  (logements / max(étages, 1), 7 classes, rouge)
          Bâti — Hauteur (étages)       (nb étages normalisé 1–20, gris clair→foncé)

        Retourne list[QgsVectorLayer] ordonnée :
          [densité, hauteur, résidentiel, religieux, château, industriel, non classé]
          Les buckets vides et la couche densité si aucun logement sont omis.

Discriminateur de chaînes : nature et usage_1 arrivent du WFS en titlecase.
La normalisation .strip().lower() est appliquée avant toute comparaison.

Dépendance QGIS uniquement — pas d'import relatif, compatible scripts Processing.
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
    QgsFeature,
    QgsField,
    QgsFillSymbol,
    QgsGraduatedSymbolRenderer,
    QgsRendererRange,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor


# =============================================================================
# Helpers privés
# =============================================================================


def _field_str(v) -> str:
    """Convertit une valeur de champ QGIS en str, '' pour None ou PyQGIS NULL."""
    if v is None:
        return ""
    s = str(v).strip()
    # str(PyQGIS NULL) == "NULL" dans toutes les versions QGIS.
    return "" if s == "NULL" else s


def _field_int(v) -> int:
    """Convertit une valeur de champ QGIS en int, 0 pour None ou PyQGIS NULL."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# Valeurs du champ `nature` (normalisées lower()) désignant un édifice religieux.
# Le WFS Géoplateforme livre ces valeurs en titlecase ; la normalisation gère ça.
_RELIGIEUX_NATURES = {"église", "chapelle"}


# =============================================================================
# Fonction publique
# =============================================================================


def build_bati_layers(buildings_layer, feedback) -> tuple:
    """
    Classifie le bâti BDTOPO selon ses attributs intrinsèques.

    Une seule itération sur buildings_layer alimente à la fois :
      - les 5 buckets de classification (règles appliquées dans l'ordre strict) ;
      - les listes pour les deux couches statistiques.

    Retourne (stats_layers, classif_layers) — deux listes de QgsVectorLayer.
    stats_layers  : [densité résidentielle, hauteur étages]
    classif_layers: [résidentiel, religieux, château, industriel, non classé]
                    — buckets vides silencieusement omis.
    """
    crs_id = buildings_layer.crs().authid()
    fields = buildings_layer.fields()

    # ── Buckets de classification ─────────────────────────────────────────────
    # Stockage direct des features (pas de second aller WFS).
    buckets = {
        "residentiel": [],
        "religieux":   [],
        "chateau":     [],
        "industriel":  [],
        "non_classe":  [],
    }

    # ── Listes statistiques ───────────────────────────────────────────────────
    density_data: list = []   # (QgsFeature, float density)
    height_data:  list = []   # (QgsFeature, int floors normalisé)

    # ── Boucle principale ─────────────────────────────────────────────────────
    for processed, feat in enumerate(buildings_layer.getFeatures()):
        if processed % 500 == 0 and feedback.isCanceled():
            return stats_layers, classif_layers

        n_logements = _field_int(feat["nombre_de_logements"])
        usage_str   = _field_str(feat["usage_1"]).lower()
        nature_str  = _field_str(feat["nature"]).lower()
        n_etages    = _field_int(feat["nombre_d_etages"])

        # NOTE : bâtiments sans données d'étages (null ou 0) normalisés à 1 plancher.
        # Ils sont visuellement indiscernables des constructions de plain-pied
        # dans la couche hauteur — limitation inhérente aux données BDTOPO.
        floors = n_etages if n_etages >= 1 else 1
        height_data.append((feat, floors))

        # ── Priorité stricte : premier match gagne ────────────────────────────
        if n_logements >= 1:
            buckets["residentiel"].append(feat)
            density = n_logements / max(n_etages, 1)
            density_data.append((feat, density))

        elif usage_str == "résidentiel":
            # usage_1 désigne explicitement le bâtiment comme résidentiel mais
            # nombre_de_logements est absent — on l'inclut sans données de densité.
            buckets["residentiel"].append(feat)

        elif nature_str in _RELIGIEUX_NATURES:
            buckets["religieux"].append(feat)

        elif nature_str == "château":
            # Les châteaux relèvent du patrimoine administratif et culturel en
            # urbanisme français ; on applique la couleur ZAI Administratif & militaire
            # (#C1121F) pour signaler leur caractère de patrimoine bâti classé.
            buckets["chateau"].append(feat)

        elif (
            usage_str == "industriel"
            or nature_str == "industriel, agricole ou commercial"
        ):
            buckets["industriel"].append(feat)

        else:
            buckets["non_classe"].append(feat)

    # ── Couches statistiques ──────────────────────────────────────────────────
    # Retournées séparément pour être placées dans un groupe dédié par l'appelant.
    stats_layers: list = []
    if density_data:
        stats_layers.append(_make_density_layer(crs_id, fields, density_data))
    if height_data:
        stats_layers.append(_make_height_layer(crs_id, fields, height_data))

    # ── Couches de classification ─────────────────────────────────────────────
    classif_layers: list = []
    _BUCKET_SPECS = [
        ("residentiel", "Bâti — Résidentiel", "#C0C0C0"),
        ("religieux",   "Bâti — Religieux",   "#9B72AA"),
        ("chateau",     "Bâti — Château",      "#C1121F"),
        ("industriel",  "Bâti — Industriel",   "#8B5E3C"),
        ("non_classe",  "Bâti — Non classé",   "#AAAAAA"),
    ]
    for bucket_key, layer_name, color_hex in _BUCKET_SPECS:
        feats = buckets[bucket_key]
        if not feats:
            continue
        classif_layers.append(_make_flat_layer(crs_id, fields, feats, layer_name, color_hex))

    return stats_layers, classif_layers


# =============================================================================
# Helpers de construction de couches
# =============================================================================


def _make_flat_layer(crs_id, fields, feats, name, color_hex):
    """Couche mémoire polygon avec remplissage uni et sans contour."""
    lyr = QgsVectorLayer(f"Polygon?crs={crs_id}", name, "memory")
    pr  = lyr.dataProvider()
    pr.addAttributes(fields.toList())
    lyr.updateFields()
    pr.addFeatures(feats)
    lyr.updateExtents()
    sym = QgsFillSymbol.createSimple({"color": color_hex, "outline_style": "no"})
    lyr.setRenderer(QgsSingleSymbolRenderer(sym))
    return lyr


def _make_density_layer(crs_id, fields, density_data):
    """
    Couche de densité résidentielle : logements / max(étages, 1).

    7 classes à coupures quantiles (distribution souvent très asymétrique :
    la plupart des bâtiments ont une densité faible). Les couleurs suivent
    la palette ColorBrewer YlOrRd-7, du jaune pâle (faible densité) au rouge
    foncé (forte densité) — lisibles à l'œil nu.

    Séquence correcte pour QgsGraduatedSymbolRenderer sur couche mémoire :
      ① addAttributes + updateFields   — le champ doit exister avant les features
      ② addFeatures (avec champ rempli)
      ③ setRenderer
    """
    name       = "Bâti — Densité résidentielle"
    field_name = "densite_log"
    n_classes  = 7

    # Palette YlOrRd ColorBrewer 7 classes — contraste élevé, discriminant à l'œil
    _DENSITY_PALETTE = [
        QColor("#FFFFB2"), QColor("#FED976"), QColor("#FEB24C"), QColor("#FD8D3C"),
        QColor("#FC4E2A"), QColor("#E31A1C"), QColor("#800026"),
    ]

    lyr = QgsVectorLayer(f"Polygon?crs={crs_id}", name, "memory")
    pr  = lyr.dataProvider()

    # ①
    pr.addAttributes(fields.toList() + [QgsField(field_name, QVariant.Double)])
    lyr.updateFields()

    lyr_fields = lyr.fields()

    # ②  Collecte des valeurs pour calculer les coupures quantiles
    new_feats = []
    values    = []
    for orig_feat, density in density_data:
        values.append(density)
        nf = QgsFeature(lyr_fields)
        nf.setGeometry(orig_feat.geometry())
        nf.setAttributes(list(orig_feat.attributes()) + [density])
        new_feats.append(nf)

    pr.addFeatures(new_feats)
    lyr.updateExtents()

    # ③ Coupures quantiles — chaque classe contient environ le même nombre
    #    de bâtiments, ce qui garantit que toutes les couleurs sont visibles.
    values.sort()
    n = len(values)
    breaks = [values[min(int(i * n / n_classes), n - 1)] for i in range(n_classes)]
    breaks.append(values[-1])   # borne supérieure inclusive

    ranges = []
    for i in range(n_classes):
        lower = breaks[i]
        upper = breaks[i + 1]
        c     = _DENSITY_PALETTE[i]
        sym   = QgsFillSymbol.createSimple({
            "color":         f"{c.red()},{c.green()},{c.blue()},255",
            "outline_style": "no",
        })
        ranges.append(QgsRendererRange(lower, upper, sym, f"{lower:.1f} – {upper:.1f}"))

    lyr.setRenderer(QgsGraduatedSymbolRenderer(field_name, ranges))
    return lyr


def _make_height_layer(crs_id, fields, height_data):
    """
    Couche de hauteur normalisée : étages null/0 → 1, étages > 20 → 20.

    Une classe par palier de 1 à min(max_floors, 20).
    Dégradé gris : lightest (#C0C0C0 · 1 étage ≈ bâti de base) → darkest (hauteur max).

    NOTE : les bâtiments sans données d'étages (null ou 0) sont normalisés à 1
    et reçoivent le même rendu que les constructions de plain-pied réelles.
    Pas de distinction possible sans donnée source.

    Séquence : ① addAttributes + updateFields  ② addFeatures  ③ setRenderer.
    """
    name       = "Bâti — Hauteur (étages)"
    field_name = "nb_etages_norm"

    lyr = QgsVectorLayer(f"Polygon?crs={crs_id}", name, "memory")
    pr  = lyr.dataProvider()

    # ①
    pr.addAttributes(fields.toList() + [QgsField(field_name, QVariant.Int)])
    lyr.updateFields()

    lyr_fields     = lyr.fields()
    max_floors_cap = 0
    new_feats      = []

    # ②
    for orig_feat, floors in height_data:
        norm = min(floors, 20)   # plafonné à 20 pour le gradient
        if norm > max_floors_cap:
            max_floors_cap = norm
        nf = QgsFeature(lyr_fields)
        nf.setGeometry(orig_feat.geometry())
        nf.setAttributes(list(orig_feat.attributes()) + [norm])
        new_feats.append(nf)

    pr.addFeatures(new_feats)
    lyr.updateExtents()

    # ③ Une classe par niveau (ranges [0.5,1.5], [1.5,2.5] …) — chaque entier
    #    tombe au centre de sa plage, sans ambiguïté de frontière.
    #    Interpolation linéaire #C0C0C0 (1 étage, visible) → #303030 (≥ 20 étages).
    #    generate_gradient part de v=1.0 (blanc) ce qui rend les 1-étage invisibles ;
    #    on calcule donc les couleurs directement.
    n_classes = max(max_floors_cap, 1)
    ranges    = []
    for i in range(n_classes):
        floor_val = i + 1
        t         = i / max(n_classes - 1, 1)   # 0.0 (1 floor) → 1.0 (max floor)
        v         = int(192 - t * 144)           # 192 (#C0) → 48 (#30)
        lower     = float(floor_val) - 0.5
        upper     = float(floor_val) + 0.5
        sym       = QgsFillSymbol.createSimple({
            "color":         f"{v},{v},{v},255",
            "outline_style": "no",
        })
        ranges.append(QgsRendererRange(lower, upper, sym, str(floor_val)))

    lyr.setRenderer(QgsGraduatedSymbolRenderer(field_name, ranges))
    return lyr
