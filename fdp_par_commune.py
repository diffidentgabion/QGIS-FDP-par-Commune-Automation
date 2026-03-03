# -*- coding: utf-8 -*-
"""
FDP par Commune — Génération automatique d'un fond de plan communal
Script QGIS Processing Toolbox

Installation :
    Traitement > Options > Traitement > Scripts > Dossiers des scripts
    → pointer vers le dossier contenant ce fichier, puis recharger les fournisseurs.
"""

import csv
import importlib.util
import io
import json
import os
import traceback

import processing
import requests
from osgeo import ogr
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFillSymbol,
    QgsGeometry,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsPointXY,
    QgsProcessingAlgorithm,
    QgsProcessingParameterString,
    QgsProject,
    QgsRasterLayer,
    QgsRuleBasedRenderer,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import Qt, QMetaType, QVariant
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from qgis.gui import QgsColorButton

# =============================================================================
# Chargement du module helper sirene_buildings
# =============================================================================
# Les scripts Processing QGIS n'ont pas de __package__ défini, donc les imports
# relatifs échouent. On charge le fichier voisin via importlib avec son chemin
# absolu, ce qui fonctionne quel que soit l'emplacement du script.
_sb_spec = importlib.util.spec_from_file_location(
    "sirene_buildings",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sirene_buildings.py"),
)
_sb_mod = importlib.util.module_from_spec(_sb_spec)
_sb_spec.loader.exec_module(_sb_mod)
build_activity_layers = _sb_mod.build_activity_layers
SIRENE_CATEGORIES     = _sb_mod.SIRENE_CATEGORIES
del _sb_spec, _sb_mod

_zb_spec = importlib.util.spec_from_file_location(
    "zone_buildings",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "zone_buildings.py"),
)
_zb_mod = importlib.util.module_from_spec(_zb_spec)
_zb_spec.loader.exec_module(_zb_mod)
build_zone_activity_layers = _zb_mod.build_zone_activity_layers
build_outdoor_space_layers = _zb_mod.build_outdoor_space_layers
_ZB_OUTDOOR_PUBLIC         = _zb_mod._OUTDOOR_PUBLIC
del _zb_spec, _zb_mod

_bb_spec = importlib.util.spec_from_file_location(
    "bati_buildings",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bati_buildings.py"),
)
_bb_mod = importlib.util.module_from_spec(_bb_spec)
_bb_spec.loader.exec_module(_bb_mod)
build_bati_layers = _bb_mod.build_bati_layers
del _bb_spec, _bb_mod

_sd_spec = importlib.util.spec_from_file_location(
    "sirene_display",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sirene_display.py"),
)
_sd_mod = importlib.util.module_from_spec(_sd_spec)
_sd_spec.loader.exec_module(_sd_mod)
build_displaced_sirene_layer = _sd_mod.build_displaced_sirene_layer
del _sd_spec, _sd_mod

# =============================================================================
# Catalogue des couches et styles par défaut
# =============================================================================

# Ordre du catalogue = ordre initial haut → bas dans la légende QGIS.
# Chaque entry est un dict figé ; le dialogue en fait une copie mutable.
_LAYER_CATALOGUE = [
    # ── Couches par défaut ────────────────────────────────────────────────────
    # Ordre = haut → bas dans la légende (haut = rendu par-dessus)
    {"section": "default",     "typename": None,                                            "display_name": "Établissements SIRENE",       "style_key": "sirene",           "geom_type": "point",   "checked": True},
    {"section": "default",     "typename": "ADMINEXPRESS-COG-CARTO.LATEST:commune",         "display_name": "Commune (limite)",            "style_key": "commune_boundary", "geom_type": "polygon", "checked": True},
    {"section": "default",     "typename": "BDTOPO_V3:zone_de_vegetation",                  "display_name": "Végétation",                  "style_key": "vegetation",       "geom_type": "polygon", "checked": True},
    {"section": "default",     "typename": "BDTOPO_V3:batiment",                            "display_name": "Bâti",                        "style_key": "buildings",        "geom_type": "polygon", "checked": True},
    {"section": "default",     "typename": "BDTOPO_V3:troncon_de_route",                    "display_name": "Voirie",                      "style_key": "roads",            "geom_type": "line",    "checked": True},
    {"section": "default",     "typename": "BDTOPO_V3:troncon_de_voie_ferree",              "display_name": "Voie ferrée",                 "style_key": "railways",         "geom_type": "line",    "checked": True},
    {"section": "default",     "typename": "BDTOPO_V3:equipement_de_transport",             "display_name": "Équipements de transport",    "style_key": "equipement_de_transport",  "geom_type": "polygon", "checked": True},
    {"section": "default",     "typename": "BDTOPO_V3:piste_d_aerodrome",                   "display_name": "Piste d'aérodrome",           "style_key": "piste_d_aerodrome","geom_type": "polygon", "checked": True},
    {"section": "default",     "typename": "BDTOPO_V3:aerodrome",                           "display_name": "Aérodrome",                   "style_key": "aerodrome",        "geom_type": "polygon", "checked": True},
    {"section": "default",     "typename": "BDTOPO_V3:surface_hydrographique",              "display_name": "Hydrographie - surface",       "style_key": "water_surface",    "geom_type": "polygon", "checked": True},
    {"section": "default",     "typename": "BDTOPO_V3:cours_d_eau",                         "display_name": "Hydrographie - cours d'eau",  "style_key": "rivers",           "geom_type": "line",    "checked": True},
    {"section": "default",     "typename": "BDTOPO_V3:zone_d_activite_ou_d_interet",        "display_name": "Zones d'activité et d'intérêt","style_key": "zai",              "geom_type": "polygon", "checked": True},
    {"section": "default",     "typename": "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle", "display_name": "Parcelles cadastrales",       "style_key": "parcels",          "geom_type": "polygon", "checked": True},
    # ── Couches recommandées ──────────────────────────────────────────────────
    {"section": "recommended", "typename": "BDTOPO_V3:erp",                            "display_name": "ERP",                          "style_key": "erp",                      "geom_type": "polygon", "checked": False},
    {"section": "recommended", "typename": "BDTOPO_V3:construction_surfacique",        "display_name": "Constructions surfaciques",     "style_key": "construction_surfacique",  "geom_type": "polygon", "checked": False},
    {"section": "recommended", "typename": "BDTOPO_V3:itineraire_autre",               "display_name": "Itinéraires (vélo, pédestre)", "style_key": "itineraire_autre",         "geom_type": "line",    "checked": False},
    {"section": "recommended", "typename": "BDTOPO_V3:haie",                           "display_name": "Haies",                        "style_key": "haie",                     "geom_type": "line",    "checked": False},
    {"section": "recommended", "typename": "BDTOPO_V3:cimetiere",                      "display_name": "Cimetières",                   "style_key": "cimetiere",                "geom_type": "polygon", "checked": False},
    {"section": "recommended", "typename": "BDTOPO_V3:detail_hydrographique",          "display_name": "Détails hydrographiques",      "style_key": "detail_hydrographique",    "geom_type": "point",   "checked": False},
    {"section": "recommended", "typename": "BDTOPO_V3:foret_publique",                 "display_name": "Forêts publiques",             "style_key": "foret_publique",           "geom_type": "polygon", "checked": False},
    # ── Couches avancées ──────────────────────────────────────────────────────
    {"section": "advanced",    "typename": "BDTOPO_V3:canalisation",                   "display_name": "Canalisation",                 "style_key": "canalisation",             "geom_type": "line",    "checked": False},
    {"section": "advanced",    "typename": "BDTOPO_V3:construction_lineaire",          "display_name": "Construction linéaire",        "style_key": "construction_lineaire",    "geom_type": "line",    "checked": False},
    {"section": "advanced",    "typename": "BDTOPO_V3:construction_ponctuelle",        "display_name": "Construction ponctuelle",      "style_key": "construction_ponctuelle",  "geom_type": "point",   "checked": False},
    {"section": "advanced",    "typename": "BDTOPO_V3:detail_orographique",            "display_name": "Détail orographique",          "style_key": "detail_orographique",      "geom_type": "point",   "checked": False},
    {"section": "advanced",    "typename": "BDTOPO_V3:lieu_dit_non_habite",            "display_name": "Lieu-dit non habité",          "style_key": "lieu_dit_non_habite",      "geom_type": "point",   "checked": False},
    {"section": "advanced",    "typename": "BDTOPO_V3:point_de_repere",                "display_name": "Point de repère",              "style_key": "point_de_repere",          "geom_type": "point",   "checked": False},
    {"section": "advanced",    "typename": "BDTOPO_V3:pylone",                         "display_name": "Pylône",                       "style_key": "pylone",                   "geom_type": "point",   "checked": False},
    {"section": "advanced",    "typename": "BDTOPO_V3:reservoir",                      "display_name": "Réservoir",                    "style_key": "reservoir",                "geom_type": "polygon", "checked": False},
    {"section": "advanced",    "typename": "BDTOPO_V3:terrain_de_sport",               "display_name": "Terrain de sport",             "style_key": "terrain_de_sport",         "geom_type": "polygon", "checked": False},
    # ── Fond de référence ─────────────────────────────────────────────────────
    {"section": "default",     "typename": None,                                            "display_name": "OpenStreetMap",               "style_key": "osm",              "geom_type": "raster",  "checked": False},
]

# Styles par défaut : style_key → dict de valeurs prêtes à l'emploi.
# QColor avec canal alpha pour l'opacité du remplissage.
# Clé "sirene" → None (rendu règle-par-règle, non éditable ici).
_DEFAULT_STYLES = {
    # ── Couches par défaut ────────────────────────────────────────────────────
    "sirene":           None,
    "buildings":        {"geom_type": "polygon", "fill_color": QColor(192, 192, 192, 255), "outline_color": QColor("#aaaaaa"), "outline_width": 0.1, "outline_style": "none"},
    "roads":            {"geom_type": "line",    "line_color": QColor("#ffffff"),           "line_width": 0.5,  "line_style": "solid"},
    "railways":         {"geom_type": "line",    "line_color": QColor("#666666"),           "line_width": 0.7,  "line_style": "solid"},
    "aerodrome":         {"geom_type": "polygon", "fill_color": QColor(210, 210, 200, 180), "outline_color": QColor("#a0a090"), "outline_width": 0.3, "outline_style": "solid"},
    "piste_d_aerodrome": {"geom_type": "polygon", "fill_color": QColor(130, 130, 120, 220), "outline_color": QColor("#606058"), "outline_width": 0.2, "outline_style": "solid"},
    "vegetation":       {"geom_type": "polygon", "fill_color": QColor(200, 230, 196, 255),  "outline_color": QColor("#aaaaaa"), "outline_width": 0.1, "outline_style": "none"},
    "rivers":           {"geom_type": "line",    "line_color": QColor("#6baed6"),           "line_width": 0.8,  "line_style": "solid"},
    "water_surface":    {"geom_type": "polygon", "fill_color": QColor(170, 211, 223, 255),  "outline_color": QColor("#6baed6"), "outline_width": 0.1, "outline_style": "none"},
    "parcels":          {"geom_type": "polygon", "fill_color": QColor(224, 224, 224, 255),  "outline_color": QColor("#cccccc"), "outline_width": 0.1, "outline_style": "none"},
    "commune_boundary": {"geom_type": "polygon", "fill_color": QColor(0,   0,   0,   0),   "outline_color": QColor("#000000"), "outline_width": 0.5, "outline_style": "solid"},
    # ── Couches recommandées ──────────────────────────────────────────────────
    "erp":                    {"geom_type": "polygon", "fill_color": QColor(244, 162,  97, 178), "outline_color": QColor("#e76f51"), "outline_width": 0.3, "outline_style": "solid"},
    "construction_surfacique":{"geom_type": "polygon", "fill_color": QColor(208, 208, 208, 255), "outline_color": QColor("#aaaaaa"), "outline_width": 0.2, "outline_style": "solid"},
    "itineraire_autre":       {"geom_type": "line",    "line_color": QColor("#2a9d8f"),           "line_width": 0.8,  "line_style": "dashed"},
    "haie":                   {"geom_type": "line",    "line_color": QColor("#52b788"),           "line_width": 0.6,  "line_style": "solid"},
    "cimetiere":              {"geom_type": "polygon", "fill_color": QColor(216, 232, 216, 255),  "outline_color": QColor("#aaaaaa"), "outline_width": 0.3, "outline_style": "solid"},
    "detail_hydrographique":  {"geom_type": "point",   "marker_color": QColor("#6baed6"),         "marker_size": 2.0},
    "foret_publique":         {"geom_type": "polygon", "fill_color": QColor(149, 201, 158, 204),  "outline_color": QColor("#52b788"), "outline_width": 0.3, "outline_style": "solid"},
    # ── Couches avancées ──────────────────────────────────────────────────────
    "canalisation":            {"geom_type": "line",    "line_color": QColor("#4fc3f7"),           "line_width": 0.5,  "line_style": "solid"},
    "construction_lineaire":   {"geom_type": "line",    "line_color": QColor("#888888"),           "line_width": 0.5,  "line_style": "solid"},
    "construction_ponctuelle": {"geom_type": "point",   "marker_color": QColor("#888888"),         "marker_size": 2.0},
    "detail_orographique":     {"geom_type": "point",   "marker_color": QColor("#b5838d"),         "marker_size": 2.0},
    "lieu_dit_non_habite":     {"geom_type": "point",   "marker_color": QColor("#555555"),         "marker_size": 2.0},
    "point_de_repere":         {"geom_type": "point",   "marker_color": QColor("#e63946"),         "marker_size": 2.0},
    "pylone":                  {"geom_type": "point",   "marker_color": QColor("#aaaaaa"),         "marker_size": 2.0},
    "reservoir":               {"geom_type": "polygon", "fill_color": QColor(144, 224, 239, 204),  "outline_color": QColor("#6baed6"), "outline_width": 0.3, "outline_style": "solid"},
    "terrain_de_sport":        {"geom_type": "polygon", "fill_color": QColor(168, 218, 220, 204),  "outline_color": QColor("#aaaaaa"), "outline_width": 0.2, "outline_style": "solid"},
    "zai":                     None,   # rendu règle-par-règle via _apply_zai_style, non éditable dans le dialogue
    "equipement_de_transport": None,   # rendu par sublayers via build_transport_layers, non éditable dans le dialogue
}


# =============================================================================
# Équipements de transport — couleurs par label (natd || nat)
# =============================================================================
# Valeurs observées sur le WFS Géoplateforme (échantillon 500 entités, 184 707 total).

_TRANSPORT_COLORS = {
    # ── Routes & intersections (bleu-gris — neutre mais caractère) ────────────
    "Péage":                      "#B0BEC5",   # bleu-gris clair — péage visible
    "Carrefour":                  "#90A4AE",   # bleu-gris moyen-clair
    "Rond-point":                 "#607D8B",   # bleu-gris moyen
    "Echangeur partiel":          "#546E7A",   # bleu-gris moyen-foncé
    "Echangeur":                  "#37474F",   # bleu-gris foncé
    # ── Ferroviaire & urbain (rouges/oranges + violet pour transit urbain) ────
    "Station de tramway":         "#AB47BC",   # violet moyen — tramway
    "Station de métro":           "#7B1FA2",   # violet foncé — métro
    "Gare routière":              "#FFA000",   # ambre vif — autocar
    "Gare RER":                   "#E53935",   # rouge vif — RER
    "Gare voyageurs uniquement":  "#C62828",   # rouge foncé — grande gare
    "Gare voyageurs et fret":     "#E64A19",   # orange-rouge — mixte
    "Gare fret uniquement":       "#BF360C",   # orange brûlé foncé — fret
    "Aire de triage":             "#4E342E",   # brun très foncé — industriel
    # ── Ports (bleus vifs) ───────────────────────────────────────────────────
    "Port de plaisance":          "#29B6F6",   # bleu ciel vif — loisir
    "Port":                       "#1565C0",   # bleu marine moyen
    "Port de commerce":           "#0D47A1",   # bleu marine foncé — commerce
    # ── Stationnement ────────────────────────────────────────────────────────
    "Parking":                    "#5C6BC0",   # indigo — distinct des autres groupes
}


def build_transport_layers(equip_layer, feedback) -> list:
    """
    Génère des couches mémoire pour les équipements de transport,
    séparées par label = nature_detaillee si non vide, sinon nature.

    Retourne list[QgsVectorLayer], ordonnées par _TRANSPORT_COLORS puis
    par ordre alphabétique pour les labels inconnus.
    """
    # ── Étape 1 : grouper les fids par label ─────────────────────────────────
    label_fids = {}
    for processed, feat in enumerate(equip_layer.getFeatures()):
        if processed % 500 == 0 and feedback.isCanceled():
            return []
        nat_str  = (feat["nature"]           or "").strip()
        natd_str = (feat["nature_detaillee"] or "").strip()
        if nat_str  == "NULL":  nat_str  = ""
        if natd_str == "NULL":  natd_str = ""
        label = natd_str if natd_str else nat_str
        if not label:
            continue
        label_fids.setdefault(label, []).append(feat.id())

    if not label_fids:
        return []

    # ── Étape 2 : couche mémoire par label ───────────────────────────────────
    crs_id = equip_layer.crs().authid()
    fields = equip_layer.fields()
    results = []

    known   = [l for l in _TRANSPORT_COLORS if l in label_fids]
    unknown = sorted(l for l in label_fids if l not in _TRANSPORT_COLORS)

    for label in known + unknown:
        fids      = label_fids[label]
        color_hex = _TRANSPORT_COLORS.get(label, "#AAAAAA")

        mem_layer = QgsVectorLayer(f"Polygon?crs={crs_id}", label, "memory")
        pr = mem_layer.dataProvider()
        pr.addAttributes(fields.toList())
        mem_layer.updateFields()
        pr.addFeatures(list(equip_layer.getFeatures(
            QgsFeatureRequest().setFilterFids(fids)
        )))
        mem_layer.updateExtents()

        c = QColor(color_hex)
        sym = QgsFillSymbol.createSimple({
            "color":         f"{c.red()},{c.green()},{c.blue()},{c.alpha()}",
            "outline_style": "no",
        })
        mem_layer.setRenderer(QgsSingleSymbolRenderer(sym))
        results.append(mem_layer)

    return results


# =============================================================================
# Algorithme principal
# =============================================================================


class FDPParCommune(QgsProcessingAlgorithm):
    """Charge automatiquement un fond de plan complet pour une commune française."""

    NOM_COMMUNE = "NOM_COMMUNE"

    # ── Métadonnées Processing ────────────────────────────────────────────────

    def flags(self):
        # FlagNoThreading oblige QGIS à exécuter cet algorithme dans le thread
        # principal de Qt, ce qui est nécessaire pour afficher des boîtes de
        # dialogue Qt (QDialog, QMessageBox, QFileDialog) en toute sécurité.
        return super().flags() | QgsProcessingAlgorithm.FlagNoThreading

    def name(self):
        return "fdp_par_commune"

    def displayName(self):
        return "FDP par Commune"

    def group(self):
        return "Fond de Plan"

    def groupId(self):
        return "fond_de_plan"

    def shortHelpString(self):
        return (
            "Génère un fond de plan communal à partir de sources ouvertes :\n"
            "  • IGN Géoplateforme WFS (ADMIN EXPRESS, Cadastre, BD TOPO)\n"
            "  • Géo-SIRENE (établissements)\n\n"
            "Saisissez un nom de commune (recherche partielle acceptée)."
        )

    def createInstance(self):
        return FDPParCommune()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterString(
                self.NOM_COMMUNE,
                "Nom de la commune",
                defaultValue="",
            )
        )

    # ── Point d'entrée ────────────────────────────────────────────────────────

    def processAlgorithm(self, parameters, context, feedback):

        # ── 1. Recherche et sélection de la commune ──────────────────────────
        nom_input = self.parameterAsString(parameters, self.NOM_COMMUNE, context)
        feedback.pushInfo(f"Recherche de la commune : {nom_input}…")

        commune = self._search_commune(nom_input)
        if commune is None:
            raise Exception("Aucune commune sélectionnée. Traitement annulé.")

        nom = commune["nom"]
        insee = commune["code"]
        dep = self._get_dep(insee)
        feedback.pushInfo(
            f"Commune : {nom}  |  INSEE : {insee}  |  Département : {dep}"
        )
        feedback.setProgress(5)

        # ── 2. Géométrie communale reprojetée en EPSG:2154 ───────────────────
        crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
        crs_2154 = QgsCoordinateReferenceSystem("EPSG:2154")
        xform = QgsCoordinateTransform(crs_4326, crs_2154, QgsProject.instance())

        # Le contour renvoyé par l'API Géo est en GeoJSON / EPSG:4326
        commune_geom = self._geojson_to_qgsgeometry(commune["geometry"])
        commune_geom.transform(xform)
        bbox = commune_geom.boundingBox()
        feedback.pushInfo(f"Emprise Lambert 93 : {bbox.toString(0)}")

        # Couche limite unique réutilisée pour tous les découpages
        boundary_layer = self._geom_to_temp_layer(commune_geom, "Polygon", crs_2154)
        feedback.setProgress(10)

        # ── 2.5. Sélection des couches et édition des styles ─────────────────
        dlg_sel = _LayerSelectorDialog()
        if dlg_sel.exec_() != QDialog.Accepted:
            raise Exception("Sélection des couches annulée.")
        selected_entries = dlg_sel.result_layers
        if not selected_entries:
            raise Exception("Aucune couche sélectionnée.")

        # ── 3. Chargement des couches WFS ────────────────────────────────────
        loaded_layers = {}  # style_key → QgsVectorLayer
        wfs_entries = [e for e in selected_entries if e["typename"] is not None]
        sirene_entry = next(
            (e for e in selected_entries if e["style_key"] == "sirene"), None
        )
        total_layers = len(wfs_entries) + (1 if sirene_entry else 0)
        progress_per_layer = 40 / max(total_layers, 1)

        for i, entry in enumerate(wfs_entries):
            if feedback.isCanceled():
                return {}
            feedback.pushInfo(f"Chargement : {entry['display_name']}…")
            layer = self._load_wfs_layer(
                entry["typename"], entry["display_name"],
                bbox, boundary_layer, crs_2154, feedback,
            )
            if layer:
                loaded_layers[entry["style_key"]] = layer
            feedback.setProgress(10 + int((i + 1) * progress_per_layer))

        # ── 4. Établissements SIRENE ──────────────────────────────────────────
        if sirene_entry and not feedback.isCanceled():
            feedback.pushInfo("Chargement des établissements SIRENE…")
            sirene_layer = self._load_sirene(insee, boundary_layer, crs_2154, feedback)
            if sirene_layer:
                loaded_layers["sirene"] = sirene_layer
        feedback.setProgress(80)

        # ── 4a-pré. Déplacement des points SIRENE autour des centroïdes bâtiment ─
        if (
            "sirene" in loaded_layers
            and "buildings" in loaded_layers
            and not feedback.isCanceled()
        ):
            feedback.pushInfo("Calcul des positions SIRENE déplacées…")
            loaded_layers["sirene"] = build_displaced_sirene_layer(
                loaded_layers["sirene"],
                loaded_layers["buildings"],
                feedback,
            )

        # ── 4a. Filtrage des ZAI fictives ─────────────────────────────────────
        # L'attribut 'fictif' est une chaîne "Vrai"/"Faux" dans BDTOPO WFS.
        # On supprime en place les entités fictives sur la couche mémoire découpée.
        zai_layer = loaded_layers.get("zai")
        if zai_layer:
            ids_to_delete = [
                feat.id() for feat in zai_layer.getFeatures()
                if str(feat["fictif"] or "").strip() == "Vrai"
            ]
            if ids_to_delete:
                zai_layer.dataProvider().deleteFeatures(ids_to_delete)
                feedback.pushInfo(
                    f"{len(ids_to_delete)} ZAI fictive(s) supprimée(s)."
                )

        # ── 4b. Couches bâtiments colorées par activité SIRENE ───────────────
        # build_activity_layers() fait le spatial join SIRENE × bâtiments et
        # retourne une couche mémoire par catégorie NAF peuplée (≥ 1 bâtiment).
        # On ne lance le calcul que si les deux couches sources sont présentes.
        activity_layers = []
        if (
            not feedback.isCanceled()
            and "sirene" in loaded_layers
            and "buildings" in loaded_layers
        ):
            feedback.pushInfo("Calcul des bâtiments par activité SIRENE…")
            activity_layers = build_activity_layers(
                loaded_layers["buildings"], loaded_layers["sirene"], feedback
            )
            feedback.pushInfo(
                f"{len(activity_layers)} couche(s) d'activité SIRENE générée(s)."
            )

        # ── 4c. Couches bâtiments colorées par zone d'activité (ZAI) ─────────
        zone_layers = []
        if (
            not feedback.isCanceled()
            and zai_layer
            and "buildings" in loaded_layers
        ):
            feedback.pushInfo("Génération des bâtiments par zone d'activité…")
            zone_layers = build_zone_activity_layers(
                loaded_layers["buildings"], zai_layer, feedback
            )
            n_cat = len({lbl for lbl, _ in zone_layers})
            feedback.pushInfo(
                f"{len(zone_layers)} couche(s) de zones générée(s) "
                f"sur {n_cat} catégorie(s) ZAI."
            )

        # ── 4c-bis. Espaces publics extérieurs (parcs, places, squares…) ───────
        outdoor_layers = []
        if zai_layer and not feedback.isCanceled():
            feedback.pushInfo("Génération des espaces publics extérieurs…")
            outdoor_layers = build_outdoor_space_layers(zai_layer, feedback)
            feedback.pushInfo(
                f"{len(outdoor_layers)} couche(s) d'espaces publics générée(s)."
            )
            # Supprimer ces entités de la couche ZAI de base pour éviter le doublon.
            # Mêmme logique de label que dans zone_buildings.py (natd || nat).
            if outdoor_layers:
                ids_outdoor = []
                for feat in zai_layer.getFeatures():
                    natd = str(feat["nature_detaillee"] or "").strip()
                    nat  = str(feat["nature"]           or "").strip()
                    if natd == "NULL": natd = ""
                    if nat  == "NULL": nat  = ""
                    label = natd if natd else nat
                    if label in _ZB_OUTDOOR_PUBLIC:
                        ids_outdoor.append(feat.id())
                if ids_outdoor:
                    zai_layer.dataProvider().deleteFeatures(ids_outdoor)
                    feedback.pushInfo(
                        f"{len(ids_outdoor)} espace(s) public(s) retiré(s) de la couche ZAI."
                    )

        # ── 4d. Couches équipements de transport colorées par type ───────────
        transport_layers = []
        equip_layer = loaded_layers.get("equipement_de_transport")
        if equip_layer and not feedback.isCanceled():
            feedback.pushInfo("Génération des équipements de transport par type…")
            transport_layers = build_transport_layers(equip_layer, feedback)
            feedback.pushInfo(
                f"{len(transport_layers)} couche(s) d'équipements générée(s)."
            )

        # ── 4e. Fond OSM optionnel ────────────────────────────────────────────
        osm_entry = next(
            (e for e in selected_entries if e["style_key"] == "osm"), None
        )
        if osm_entry and not feedback.isCanceled():
            osm_uri = (
                "type=xyz"
                "&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png"
                "&zmax=19&zmin=0"
            )
            osm_lyr = QgsRasterLayer(osm_uri, "OpenStreetMap", "wms")
            if osm_lyr.isValid():
                loaded_layers["osm"] = osm_lyr

        # ── 4f. Classification intrinsèque du bâti ───────────────────────────
        bati_stats  = []
        bati_layers = []
        if "buildings" in loaded_layers and not feedback.isCanceled():
            feedback.pushInfo("Classification intrinsèque du bâti…")
            bati_stats, bati_layers = build_bati_layers(loaded_layers["buildings"], feedback)
            feedback.pushInfo(
                f"{len(bati_layers)} couche(s) bâti intrinsèque + "
                f"{len(bati_stats)} couche(s) statistique(s) générée(s)."
            )

        # ── 5. Groupe QGIS + symbologie + ajout des couches ──────────────────
        # L'ordre du dialogue est haut → bas dans la légende.
        # group.addLayer() ajoute en fin de liste enfants, donc le premier entry
        # se retrouve à l'index 0 (sommet = rendu par-dessus).
        root = QgsProject.instance().layerTreeRoot()
        group = root.insertGroup(0, nom)

        for entry in selected_entries:
            sk = entry["style_key"]
            if sk not in loaded_layers:
                continue
            layer = loaded_layers[sk]

            # ── Sous-groupes programmatiques ─────────────────────────────────
            if sk == "buildings":
                # Données statistiques EN PREMIER → juste sous Végétation dans la légende
                if bati_stats:
                    bati_data_grp = group.addGroup("Bâti — Données")
                    for b_layer in bati_stats:
                        QgsProject.instance().addMapLayer(b_layer, False)
                        bati_data_grp.addLayer(b_layer)
                if zone_layers:
                    zone_grp = group.addGroup("Bâti par zone d'activité")
                    cat_subgroups = {}
                    for cat_label, z_layer in zone_layers:
                        if cat_label not in cat_subgroups:
                            cat_subgroups[cat_label] = zone_grp.addGroup(cat_label)
                        QgsProject.instance().addMapLayer(z_layer, False)
                        cat_subgroups[cat_label].addLayer(z_layer)
                if activity_layers:
                    sirene_grp = group.addGroup("Bâti par activité SIRENE")
                    for act_layer in activity_layers:
                        QgsProject.instance().addMapLayer(act_layer, False)
                        sirene_grp.addLayer(act_layer)
                if bati_layers:
                    bati_grp = group.addGroup("Bâti intrinsèque")
                    for b_layer in bati_layers:
                        QgsProject.instance().addMapLayer(b_layer, False)
                        bati_grp.addLayer(b_layer)

            if sk == "equipement_de_transport" and transport_layers:
                grp = group.addGroup("Équipements de transport")
                for t_layer in transport_layers:
                    QgsProject.instance().addMapLayer(t_layer, False)
                    grp.addLayer(t_layer)
                continue   # sublayers remplacent la couche plate

            # ── Symbologie ───────────────────────────────────────────────────
            if sk in ("sirene", "zai"):
                self._apply_style(layer, sk)
            elif sk == "osm":
                pass   # QgsRasterLayer — pas de symbologie vectorielle
            elif entry.get("style") is not None:
                self._apply_custom_style(layer, entry["style"], entry["geom_type"])
            else:
                self._apply_style(layer, sk)

            QgsProject.instance().addMapLayer(layer, False)
            group.addLayer(layer)

            # Espaces publics extérieurs ajoutés après la couche ZAI → apparaissent dessous dans la légende
            if sk == "zai" and outdoor_layers:
                outdoor_grp = group.addGroup("Espaces publics extérieurs")
                for o_layer in outdoor_layers:
                    QgsProject.instance().addMapLayer(o_layer, False)
                    outdoor_grp.addLayer(o_layer)

        feedback.pushInfo(
            f"{len(loaded_layers)} couche(s) chargée(s) dans le groupe « {nom} »."
        )
        feedback.setProgress(90)

        # ── 6. Proposition d'enregistrement .qgz ─────────────────────────────
        reply = QMessageBox.question(
            None,
            "Enregistrer le projet",
            f"Fond de plan « {nom} » créé avec succès.\n\n"
            "Voulez-vous enregistrer le projet en fichier .qgz ?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            default_filename = nom.replace(" ", "_") + "_basemap.qgz"
            path, _ = QFileDialog.getSaveFileName(
                None,
                "Enregistrer le projet QGIS",
                os.path.join(os.path.expanduser("~"), default_filename),
                "Projet QGIS (*.qgz)",
            )
            if path:
                QgsProject.instance().write(path)
                feedback.pushInfo(f"Projet enregistré : {path}")

        feedback.setProgress(100)
        feedback.pushInfo("Traitement terminé.")
        return {}

    # =========================================================================
    # Helper – recherche et sélection de commune
    # =========================================================================

    def _search_commune(self, nom_input):
        """
        Interroge l'API Géo gouv.fr et renvoie un dict commune, ou None si annulé.
        Le dict contient les clés : 'nom', 'code' (INSEE), 'geometry' (GeoJSON).

        Stratégie multi-passes pour couvrir tous les cas courants :
          - Nom complet ou partiel (ex. "Neuilly", "Paris 19ème")
          - Code postal 5 chiffres (ex. "75019") → recherche par codePostal
          - Code INSEE (ex. "75119", "2A004") → recherche par code
        Les arrondissements municipaux (Paris, Lyon, Marseille) sont inclus
        dans toutes les passes via &type=commune-actuelle,arrondissement-municipal.
        Les résultats sont fusionnés et dédupliqués par code INSEE.
        """
        nom_input = nom_input.strip()
        _FIELDS = "fields=nom,code,contour&format=geojson&geometry=contour"
        _TYPES  = "type=commune-actuelle,arrondissement-municipal"
        _BASE   = "https://geo.api.gouv.fr/communes"

        def _fetch(url):
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            return resp.json().get("features", [])

        features   = []
        seen_codes = set()

        def _merge(new_feats):
            for feat in new_feats:
                code = feat["properties"].get("code", "")
                if code and code not in seen_codes:
                    seen_codes.add(code)
                    features.append(feat)

        try:
            # Passe 1 — recherche par nom (toujours)
            _merge(_fetch(
                f"{_BASE}?nom={requests.utils.quote(nom_input)}&{_FIELDS}&{_TYPES}"
            ))

            # Passe 2 — si l'entrée ressemble à un code postal (5 chiffres)
            if nom_input.isdigit() and len(nom_input) == 5:
                _merge(_fetch(
                    f"{_BASE}?codePostal={nom_input}&{_FIELDS}&{_TYPES}"
                ))

            # Passe 3 — si l'entrée ressemble à un code INSEE (4–5 chars alphanum)
            # Couvre les codes normaux (ex. "75119"), la Corse ("2A004"), les DOM ("97209")
            if 4 <= len(nom_input) <= 5 and nom_input.replace("-", "").isalnum():
                _merge(_fetch(
                    f"{_BASE}?code={requests.utils.quote(nom_input.upper())}&{_FIELDS}&{_TYPES}"
                ))

        except requests.RequestException as e:
            raise Exception(f"Impossible de contacter l'API Géo : {e}")

        if not features:
            raise Exception(f"Aucune commune trouvée pour « {nom_input} ».")

        if len(features) == 1:
            p = features[0]["properties"]
            return {
                "nom": p["nom"],
                "code": p["code"],
                "geometry": features[0]["geometry"],
            }

        # Plusieurs résultats → dialogue de sélection
        dlg = _CommuneSelectDialog(features)
        if dlg.exec_() != QDialog.Accepted or dlg.selected_commune is None:
            return None
        return dlg.selected_commune

    # =========================================================================
    # Helper – code département depuis INSEE
    # =========================================================================

    def _get_dep(self, insee_code: str) -> str:
        """
        Dérive le code département utilisé dans le nom du fichier Géo-SIRENE.
          - Corse-du-Sud   : '2A'
          - Haute-Corse    : '2B'
          - DOM-TOM        : 3 chiffres (971–976)
          - Métropole      : 2 chiffres ('01'–'95')
        """
        if insee_code.startswith("2A"):
            return "2A"
        if insee_code.startswith("2B"):
            return "2B"
        if insee_code.startswith("97"):
            return insee_code[:3]  # ex. '974' → La Réunion
        return insee_code[:2]  # ex. '75' → Paris

    # =========================================================================
    # Helpers – géométrie
    # =========================================================================

    def _geojson_to_qgsgeometry(self, geojson_dict: dict) -> QgsGeometry:
        """
        Convertit un dict géométrie GeoJSON en QgsGeometry.
        On passe par OGR qui est toujours disponible dans une installation QGIS.
        """
        ogr_geom = ogr.CreateGeometryFromJson(json.dumps(geojson_dict))
        return QgsGeometry.fromWkt(ogr_geom.ExportToWkt())

    def _geom_to_temp_layer(
        self,
        geom: QgsGeometry,
        geom_type: str,
        crs: QgsCoordinateReferenceSystem,
    ) -> QgsVectorLayer:
        """
        Crée une couche mémoire contenant une seule entité (geom).
        Utilisée comme couche de découpage (OVERLAY) dans native:clip.
        """
        layer = QgsVectorLayer(f"{geom_type}?crs={crs.authid()}", "_boundary", "memory")
        feat = QgsFeature()
        feat.setGeometry(geom)
        layer.dataProvider().addFeature(feat)
        layer.updateExtents()
        return layer

    # =========================================================================
    # Helper – chargement WFS
    # =========================================================================

    def _load_wfs_layer(
        self,
        typename: str,
        display_name: str,
        bbox,
        boundary_layer: QgsVectorLayer,
        crs_2154: QgsCoordinateReferenceSystem,
        feedback,
    ):
        """
        Construit l'URI WFS GetFeature avec filtre BBOX, charge la couche,
        puis la découpe sur le contour communal.
        Renvoie None (avec avertissement) si la couche est vide ou invalide.
        """
        # URI WFS — le paramètre BBOX attend : minX,minY,maxX,maxY,CRS
        bbox_str = (
            f"{bbox.xMinimum()},{bbox.yMinimum()},"
            f"{bbox.xMaximum()},{bbox.yMaximum()},EPSG:2154"
        )
        uri = (
            "https://data.geopf.fr/wfs/ows"
            "?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
            f"&TYPENAME={typename}&SRSNAME=EPSG:2154&BBOX={bbox_str}"
        )

        layer = QgsVectorLayer(uri, display_name, "WFS")

        if not layer.isValid():
            feedback.pushWarning(f"  ⚠ Couche WFS invalide : {display_name}")
            return None
        if layer.featureCount() == 0:
            feedback.pushWarning(f"  ⚠ Aucune entité retournée : {display_name}")
            return None

        # Découpage sur le contour communal
        clipped = processing.run(
            "native:clip",
            {
                "INPUT": layer,
                "OVERLAY": boundary_layer,
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]
        clipped.setName(display_name)
        return clipped

    # =========================================================================
    # Helper – SIRENE
    # =========================================================================

    def _load_sirene(
        self,
        insee: str,
        boundary_layer: QgsVectorLayer,
        crs_2154: QgsCoordinateReferenceSystem,
        feedback,
    ):
        """
        Télécharge le CSV Géo-SIRENE par commune depuis files.data.gouv.fr.

        Le fichier contient uniquement les établissements de la commune — pas
        de filtrage par code commune nécessaire. Une seule requête HTTP, pas
        de décompression, pas de pagination.

        Les noms de colonnes sont normalisés en minuscules pour être robustes
        aux éventuels changements de casse dans le fichier source.
        """
        url = f"https://files.data.gouv.fr/geo-sirene/last/communes/{insee}.csv"
        feedback.pushInfo(f"  Téléchargement Géo-SIRENE commune {insee}…")

        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()

            feedback.pushInfo(
                f"  Fichier reçu ({len(resp.content) / 1024:.0f} Ko). "
                f"Traitement en cours…"
            )

            # ── Couche mémoire point (EPSG:4326) ─────────────────────────────
            mem_layer = QgsVectorLayer("Point?crs=EPSG:4326", "SIRENE_raw", "memory")
            pr = mem_layer.dataProvider()
            pr.addAttributes(
                [
                    QgsField("siret", QMetaType.Type.QString),
                    QgsField("nom", QMetaType.Type.QString),
                    QgsField("activitePrincipaleEtablissement", QMetaType.Type.QString),
                    QgsField("adresse", QMetaType.Type.QString),
                ]
            )
            mem_layer.updateFields()

            # ── Lecture du CSV ────────────────────────────────────────────────
            # Les noms de colonnes sont normalisés en minuscules une seule fois
            # sur la ligne d'en-tête pour ne pas dépendre de la casse du fichier.
            reader = csv.reader(io.StringIO(resp.content.decode("utf-8")))
            headers = [h.lower() for h in next(reader)]
            features = []

            for values in reader:
                if feedback.isCanceled():
                    return None
                row = dict(zip(headers, values))

                # ── Filtre 1 : établissements actifs uniquement ───────────────
                if row.get("etatadministratifetablissement") != "A":
                    continue

                # ── Filtre 2 : exclure sections T et U ───────────────────────
                # T (97-98) = ménages employeurs (famille avec employé de maison)
                # U (99)    = activités extraterritoriales
                # Ces codes ne correspondent pas à des entreprises au sens urbain.
                naf = row.get("activiteprincipaleetablissement", "")
                if naf and naf[:2].isdigit() and int(naf[:2]) >= 97:
                    continue

                # ── Filtre 3 : nom requis ─────────────────────────────────────
                # Priorité : enseigne (nom sur la porte) > dénomination usuelle
                # de l'établissement > dénomination de l'unité légale > nom/prénom
                # pour les entrepreneurs individuels.
                # Les entités sans aucun nom sont des holdings dormantes ou des
                # erreurs d'enregistrement — on les écarte.
                nom = (
                    row.get("enseigne1etablissement", "").strip()
                    or row.get("denominationusuelleetablissement", "").strip()
                    or row.get("denominationunitelegale", "").strip()
                    or " ".join(
                        filter(
                            None,
                            [
                                row.get("prenom1unitelegale", "").strip(),
                                row.get("nomunitelegale", "").strip(),
                            ],
                        )
                    ).strip()
                )
                if not nom:
                    continue

                # ── Filtre 4 : qualité de géolocalisation ────────────────────
                # geo_score < 0.4 = géocodage échoué, point placé au centroïde
                # de la commune ou de la rue — position non fiable.
                geo_score = row.get("geo_score", "")
                if geo_score:
                    try:
                        if float(geo_score) < 0.4:
                            continue
                    except ValueError:
                        pass  # valeur non numérique → on conserve

                # ── Géométrie ────────────────────────────────────────────────
                lat = row.get("latitude", "")
                lon = row.get("longitude", "")
                if not lat or not lon:
                    continue

                try:
                    geom = QgsGeometry.fromPointXY(QgsPointXY(float(lon), float(lat)))
                except (ValueError, TypeError):
                    continue

                # ── Adresse ──────────────────────────────────────────────────
                adresse = " ".join(
                    filter(
                        None,
                        [
                            row.get("numerovoieetablissement", ""),
                            row.get("indicerepetitionetablissement", ""),
                            row.get("typevoieetablissement", ""),
                            row.get("libellevoieetablissement", ""),
                            row.get("codepostaletablissement", ""),
                            row.get("libellecommuneetablissement", ""),
                        ],
                    )
                )

                feat = QgsFeature(mem_layer.fields())
                feat.setGeometry(geom)
                feat.setAttribute("siret", row.get("siret", ""))
                feat.setAttribute("nom", nom[:254])
                feat.setAttribute("activitePrincipaleEtablissement", naf)
                feat.setAttribute("adresse", adresse[:254])
                features.append(feat)

            pr.addFeatures(features)
            mem_layer.updateExtents()
            feedback.pushInfo(
                f"  {len(features)} établissement(s) actif(s) géolocalisé(s)."
            )

            if not features:
                feedback.pushWarning("  ⚠ Aucun établissement actif géolocalisé.")
                return None

            # ── Reprojection EPSG:4326 → EPSG:2154 ───────────────────────────
            reprojected = processing.run(
                "native:reprojectlayer",
                {
                    "INPUT": mem_layer,
                    "TARGET_CRS": crs_2154,
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]

            # ── Découpage sur le contour communal ─────────────────────────────
            clipped = processing.run(
                "native:clip",
                {
                    "INPUT": reprojected,
                    "OVERLAY": boundary_layer,
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]

            clipped.setName("Établissements SIRENE")
            return clipped

        except requests.HTTPError as e:
            feedback.pushWarning(
                f"  ⚠ Fichier Géo-SIRENE introuvable pour la commune {insee} : {e}"
            )
            return None
        except Exception as e:
            feedback.pushWarning(f"  ⚠ Erreur SIRENE : {e}\n{traceback.format_exc()}")
            return None

    # =========================================================================
    # Helpers – symbologie
    # =========================================================================

    def _apply_style(self, layer: QgsVectorLayer, style_key: str):
        """
        Applique une symbologie par défaut cohérente pour un fond de plan
        architectural : tons neutres, palette minimale.
        """
        if style_key == "sirene":
            self._apply_sirene_style(layer)
            return
        if style_key == "zai":
            self._apply_zai_style(layer)
            return

        # Chaque entrée est un callable qui renvoie un QgsSymbol configuré.
        # On utilise des lambdas pour éviter de créer des symboles inutilisés.
        style_factories = {
            # Limite communale : contour noir fin, sans remplissage
            "commune_boundary": lambda: QgsFillSymbol.createSimple(
                {
                    "color": "0,0,0,0",
                    "outline_color": "#000000",
                    "outline_width": "0.5",
                }
            ),
            # Parcelles cadastrales : remplissage gris très clair, sans contour
            "parcels": lambda: QgsFillSymbol.createSimple(
                {
                    "color": "#e0e0e0",
                    "outline_style": "no",
                }
            ),
            # Surfaces en eau : bleu clair, sans contour
            "water_surface": lambda: QgsFillSymbol.createSimple(
                {
                    "color": "#aad3df",
                    "outline_style": "no",
                }
            ),
            # Cours d'eau : ligne bleu moyen
            "rivers": lambda: QgsLineSymbol.createSimple(
                {
                    "color": "#6baed6",
                    "width": "0.8",
                }
            ),
            # Végétation : vert très pâle, sans contour
            "vegetation": lambda: QgsFillSymbol.createSimple(
                {
                    "color": "#c8e6c4",
                    "outline_style": "no",
                }
            ),
            # Voirie : ligne blanche (s'intègre au fond clair)
            "roads": lambda: QgsLineSymbol.createSimple(
                {
                    "color": "#ffffff",
                    "width": "0.5",
                }
            ),
            # Voie ferrée : ligne grise continue
            "railways": lambda: QgsLineSymbol.createSimple(
                {
                    "color": "#666666",
                    "width": "0.7",
                }
            ),
            # Bâti : gris moyen, sans contour
            "buildings": lambda: QgsFillSymbol.createSimple(
                {
                    "color": "#c0c0c0",
                    "outline_style": "no",
                }
            ),
        }

        factory = style_factories.get(style_key)
        if factory:
            layer.setRenderer(QgsSingleSymbolRenderer(factory()))
            layer.triggerRepaint()

    def _apply_sirene_style(self, layer: QgsVectorLayer):
        """
        Rendu règle par règle des établissements SIRENE.

        Les catégories s'inspirent de la Base Permanente des Équipements (BPE)
        de l'INSEE — référence standard en urbanisme français — mais sont dérivées
        des codes NAF SIRENE, source unique de données ici.

        Différence notable avec le BPE : les pharmacies (47.73Z) et opticiens
        (47.78A) apparaissent dans « Commerce » (leur section G dans SIRENE)
        plutôt que dans « Santé », car le BPE procède à un reclassement fonctionnel
        que SIRENE n'opère pas.

        Chaque catégorie a une forme et une couleur distinctes.
        Les 8 premières catégories utilisent des formes uniques (fill-based uniquement —
        les formes stroke-only comme cross/cross2 disparaissent avec outline_style:no).
        Les 4 suivantes recyclent les premières formes avec des couleurs différentes.
        """
        # Tuples : (libellé, plages_NAF, couleur, taille, forme, expr_custom)
        # expr_custom remplace _naf_div_expr(ranges) quand il est non-None.
        # Utilisé pour Éducation (exclusion des codes Formation) et Formation
        # (match exact de codes dans la section P).
        _FORMATION_CODES = (
            "'85.51Z','85.52Z','85.53Z','85.59A','85.59B','85.60Z'"
        )
        _div = 'to_int(left("activitePrincipaleEtablissement", 2))'
        groups = [
            # ── BPE domaine B : Commerce ──────────────────────────────────────
            # Inclut pharmacies (47.73Z) et opticiens (47.78A) : section G SIRENE
            ("Commerce", [(45, 47)], "#F4A261", 3.0, "circle", None),
            # ── BPE domaines G+I : Restauration & hébergement ─────────────────
            ("Restauration & hébergement", [(55, 56)], "#E63946", 3.0, "square", None),
            # ── BPE domaine D : Santé & action sociale ────────────────────────
            ("Santé & action sociale", [(86, 88)], "#06D6A0", 3.0, "diamond", None),
            # ── BPE domaine C (partiel) : Éducation ───────────────────────────
            # Division 85 sauf les codes Formation continue/artistique (85.51Z…)
            (
                "Éducation",
                [(85, 85)],
                "#FFD166",
                3.0,
                "triangle",
                f'({_div} = 85) AND "activitePrincipaleEtablissement" NOT IN ({_FORMATION_CODES})',
            ),
            # ── BPE domaine C (partiel) : Formation ───────────────────────────
            # Formation continue, artistique, sport, auto-école (85.51Z–85.60Z)
            (
                "Formation",
                [],
                "#B8A000",
                3.0,
                "star",
                f'"activitePrincipaleEtablissement" IN ({_FORMATION_CODES})',
            ),
            # ── BPE domaine H : Services publics & administration ─────────────
            # (La Poste, NAF 53.10Z, est classée ici dans Transport & logistique)
            ("Équipements & services publics", [(84, 84)], "#C1121F", 3.0, "pentagon", None),
            # ── BPE domaine F : Culture, sport & loisirs ──────────────────────
            ("Culture, sport & loisirs", [(90, 93)], "#118AB2", 3.0, "hexagon", None),
            # ── BPE domaine A : Services aux personnes & associations ──────────
            (
                "Services aux personnes & associations",
                [(94, 96)],
                "#F48FB1",
                2.5,
                "cross_fill",
                None,
            ),
            # ── Hors BPE : Bureaux & services tertiaires ──────────────────────
            # Sections J (info/comm), K (finance), L (immobilier),
            # M (conseil/ingénierie), N (services admin.)
            # Forme répétée (circle), couleur distincte (violet)
            (
                "Bureaux & services tertiaires",
                [(58, 66), (68, 75), (77, 82)],
                "#7B2D8B",
                2.5,
                "circle",
                None,
            ),
            # ── Hors BPE : Industrie, artisanat & construction ────────────────
            # Sections B (extractif), C (industrie), D (énergie),
            # E (eau/déchets), F (construction)
            # Forme répétée (square), couleur distincte (brun)
            (
                "Industrie, artisanat & construction",
                [(5, 9), (10, 43)],
                "#8B5E3C",
                2.5,
                "square",
                None,
            ),
            # ── Hors BPE : Transport & logistique ────────────────────────────
            # Forme répétée (diamond), couleur distincte (gris)
            ("Transport & logistique", [(49, 53)], "#6C757D", 2.5, "diamond", None),
            # ── Hors BPE : Agriculture ────────────────────────────────────────
            # Forme répétée (triangle), couleur distincte (vert foncé)
            ("Agriculture, sylviculture & pêche", [(1, 3)], "#2D6A4F", 2.5, "triangle", None),
        ]

        root_rule = QgsRuleBasedRenderer.Rule(None)

        for label, ranges, color, size, shape, custom_expr in groups:
            sym = QgsMarkerSymbol.createSimple(
                {
                    "color": color,
                    "name": shape,
                    "size": str(size),
                    "outline_style": "no",
                }
            )
            rule = QgsRuleBasedRenderer.Rule(sym)
            rule.setFilterExpression(
                custom_expr if custom_expr is not None else self._naf_div_expr(ranges)
            )
            rule.setLabel(label)
            root_rule.appendChild(rule)

        # Règle de repli (codes absents, malformés ou NAF inconnu)
        other_sym = QgsMarkerSymbol.createSimple(
            {
                "color": "#BBBBBB",
                "name": "circle",
                "size": "1.5",
                "outline_style": "no",
            }
        )
        other_rule = QgsRuleBasedRenderer.Rule(other_sym)
        other_rule.setFilterExpression("ELSE")
        other_rule.setLabel("Activité non classée")
        root_rule.appendChild(other_rule)

        layer.setRenderer(QgsRuleBasedRenderer(root_rule))
        layer.triggerRepaint()

    def _apply_zai_style(self, layer: QgsVectorLayer):
        """
        Rendu règle-par-règle des zones d'activité et d'intérêt (BDTOPO ZAI).

        Hypothèse : le WFS BDTOPO_V3 retourne l'attribut 'categorie' avec les
        accents et la casse d'origine (ex. "Santé", "Culture et loisirs") tels
        que documentés dans le modèle de données BDTOPO_V3. Si une valeur ne
        correspond à aucune des 8 catégories connues (ou est NULL), la règle
        ELSE s'applique avec le remplissage de repli #E8E8E8.
        """
        rules_data = [
            ("Science et enseignement",    "#FFF0B3"),
            ("Santé",                      "#B3F5E6"),
            ("Administratif ou militaire", "#F5B3B6"),
            ("Industriel et commercial",   "#E8D5C4"),
            ("Culture et loisirs",         "#B3DFF0"),
            ("Sport",                      "#FCDEC4"),
            ("Religieux",                  "#E0D0E8"),
            ("Gestion des eaux",           "#C4E3F5"),
        ]

        root_rule = QgsRuleBasedRenderer.Rule(None)

        for label, fill_color in rules_data:
            sym = QgsFillSymbol.createSimple({
                "color":         fill_color,
                "outline_color": "#888888",
                "outline_width": "0.2",
            })
            rule = QgsRuleBasedRenderer.Rule(sym)
            rule.setFilterExpression(f'"categorie" = \'{label}\'')
            rule.setLabel(label)
            root_rule.appendChild(rule)

        # Règle ELSE pour catégories inconnues / NULL
        fallback_sym = QgsFillSymbol.createSimple({
            "color":         "#E8E8E8",
            "outline_color": "#888888",
            "outline_width": "0.2",
        })
        fallback_rule = QgsRuleBasedRenderer.Rule(fallback_sym)
        fallback_rule.setFilterExpression("ELSE")
        fallback_rule.setLabel("Autre / non classé")
        root_rule.appendChild(fallback_rule)

        layer.setRenderer(QgsRuleBasedRenderer(root_rule))
        layer.triggerRepaint()

    # =========================================================================
    # Helper – symbologie personnalisée (depuis _LayerSelectorDialog)
    # =========================================================================

    def _apply_custom_style(
        self, layer: QgsVectorLayer, style: dict, geom_type: str
    ):
        """
        Applique un style issu du dialogue _LayerSelectorDialog.
        Le dict 'style' utilise des QColor avec canal alpha pour l'opacité.
        Les valeurs de largeur/taille sont en mm (float).
        """
        # Convertit le style QGIS "outline_style" en valeur attendue par
        # QgsFillSymbol / QgsLineSymbol : "solid", "dash", "no".
        _outline_map = {"solid": "solid", "dashed": "dash", "none": "no"}
        _line_map    = {"solid": "solid", "dashed": "dash"}

        if geom_type == "polygon":
            fc = style.get("fill_color", QColor(200, 200, 200, 255))
            oc = style.get("outline_color", QColor("#000000"))
            ow = style.get("outline_width", 0.3)
            os_ = _outline_map.get(style.get("outline_style", "none"), "no")
            # Encode RGBA pour que QGIS respecte l'opacité du remplissage
            color_str = f"{fc.red()},{fc.green()},{fc.blue()},{fc.alpha()}"
            sym = QgsFillSymbol.createSimple({
                "color": color_str,
                "outline_color": oc.name(),
                "outline_width": str(ow),
                "outline_style": os_,
            })

        elif geom_type == "line":
            lc = style.get("line_color", QColor("#888888"))
            lw = style.get("line_width", 0.5)
            ls_ = _line_map.get(style.get("line_style", "solid"), "solid")
            props = {"color": lc.name(), "width": str(lw)}
            if ls_ == "dash":
                props["customdash"] = "5;3"
                props["use_custom_dash"] = "1"
            sym = QgsLineSymbol.createSimple(props)

        elif geom_type == "point":
            mc = style.get("marker_color", QColor("#333333"))
            ms = style.get("marker_size", 2.0)
            sym = QgsMarkerSymbol.createSimple({
                "color": mc.name(),
                "name": "circle",
                "size": str(ms),
                "outline_style": "no",
            })

        else:
            return  # type inconnu, on laisse le style par défaut

        layer.setRenderer(QgsSingleSymbolRenderer(sym))
        layer.triggerRepaint()

    @staticmethod
    def _naf_div_expr(ranges: list) -> str:
        """
        Renvoie une expression QGIS filtrant les établissements dont le code NAF
        (format SIRENE : "DD.DDL", ex. "47.11Z") tombe dans l'une des plages de
        divisions indiquées.

        La division est l'entier formé par les 2 premiers caractères du code :
          "47.11Z" → to_int("47") = 47 → section G (Commerce).

        Chaque groupe peut couvrir plusieurs plages non contiguës, ce qui permet
        de regrouper plusieurs sections NAF en une seule catégorie BPE.
        Ex. : Bureaux & services tertiaires = [(58,66),(68,75),(77,82)]
              couvre J (58-63) + K (64-66) + L (68) + M (69-75) + N (77-82).
        """
        field = 'to_int(left("activitePrincipaleEtablissement", 2))'
        clauses = [f"({field} BETWEEN {lo} AND {hi})" for lo, hi in ranges]
        return " OR ".join(clauses) if clauses else "FALSE"


# =============================================================================
# Dialogue de sélection de commune
# =============================================================================


class _CommuneSelectDialog(QDialog):
    """
    Présente une liste de communes candidates pour que l'utilisateur
    sélectionne celle qu'il souhaite traiter.
    """

    def __init__(self, features: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sélectionner une commune")
        self.setMinimumWidth(400)
        self.selected_commune = None
        self._features = features

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                f"{len(features)} communes correspondent à votre recherche.\n"
                "Sélectionnez la commune souhaitée :"
            )
        )

        self._list = QListWidget()
        for feat in features:
            p = feat["properties"]
            nom = p["nom"]
            code = p["code"]
            # Afficher le nom et le code INSEE complet pour lever toute ambiguïté
            self._list.addItem(f"{nom}  —  {code}")
        self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(self.accept)
        layout.addWidget(self._list)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        row = self._list.currentRow()
        if row >= 0:
            feat = self._features[row]
            p = feat["properties"]
            self.selected_commune = {
                "nom": p["nom"],
                "code": p["code"],
                "geometry": feat["geometry"],
            }
        super().accept()


# =============================================================================
# Dialogue de sélection et de style des couches
# =============================================================================


class _LayerSelectorDialog(QDialog):
    """
    Dialogue affiché avant tout chargement. L'utilisateur choisit les couches
    à charger, leur ordre dans la légende, et leurs styles par défaut.

    Renvoie result_layers : liste ordonnée de dicts (haut → bas dans la légende)
    avec les clés typename, display_name, style_key, geom_type, style (dict ou None).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sélection et style des couches")
        self.setMinimumSize(1000, 640)
        self.result_layers = []

        # Copie mutable des styles : style_key → dict (modifié en temps réel)
        self._styles = {}
        for entry in _LAYER_CATALOGUE:
            sk = entry["style_key"]
            default = _DEFAULT_STYLES.get(sk)
            if default is not None:
                self._styles[sk] = dict(default)

        # Registre des checkboxes : style_key → QCheckBox
        self._checkboxes = {}

        self._build_ui()
        self._populate_order_list()

    # ── Construction de l'interface ───────────────────────────────────────────

    def _build_ui(self):
        root_layout = QVBoxLayout(self)

        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter)

        # ── Panneau gauche : catalogue ────────────────────────────────────────
        left_outer = QWidget()
        left_layout = QVBoxLayout(left_outer)
        left_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(6, 6, 6, 6)

        self._build_section(scroll_layout, "Couches par défaut",    "default",     collapsible=False)
        self._build_section(scroll_layout, "Couches recommandées",  "recommended", collapsible=False)
        self._build_section(scroll_layout, "Couches avancées",      "advanced",    collapsible=True)
        scroll_layout.addStretch()

        scroll.setWidget(scroll_content)
        left_layout.addWidget(scroll)
        splitter.addWidget(left_outer)

        # ── Panneau droit : ordre + éditeur de style ──────────────────────────
        right_outer = QWidget()
        right_layout = QVBoxLayout(right_outer)
        splitter.addWidget(right_outer)

        # Liste d'ordre
        order_group = QGroupBox("Ordre des couches  (haut = rendu par-dessus)")
        order_vbox = QVBoxLayout(order_group)

        self._order_list = QListWidget()
        self._order_list.setDragDropMode(QAbstractItemView.InternalMove)
        order_vbox.addWidget(self._order_list)

        arrow_layout = QHBoxLayout()
        self._btn_up   = QPushButton("▲ Monter")
        self._btn_down = QPushButton("▼ Descendre")
        arrow_layout.addWidget(self._btn_up)
        arrow_layout.addWidget(self._btn_down)
        order_vbox.addLayout(arrow_layout)
        right_layout.addWidget(order_group, stretch=2)

        # Éditeur de style
        self._style_group = QGroupBox("Style de la couche sélectionnée")
        self._style_vbox  = QVBoxLayout(self._style_group)
        # _style_content est le seul enfant direct de _style_vbox.
        # On le remplace en entier (replaceWidget) plutôt que de modifier son
        # contenu widget par widget — cela évite le clignotement causé par
        # deleteLater() qui est asynchrone et laisse les anciens widgets visibles
        # le temps que le prochain tour d'event loop les supprime.
        self._style_content = QWidget()
        QVBoxLayout(self._style_content).addWidget(
            QLabel("Sélectionnez une couche dans la liste.")
        )
        self._style_vbox.addWidget(self._style_content)
        right_layout.addWidget(self._style_group, stretch=3)

        # Boutons OK / Annuler
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root_layout.addWidget(buttons)

        splitter.setSizes([380, 620])

        # Connexions
        self._btn_up.clicked.connect(lambda: self._move_row(-1))
        self._btn_down.clicked.connect(lambda: self._move_row(1))
        self._order_list.currentRowChanged.connect(self._on_selection_changed)

    def _build_section(self, parent_layout, title, section, collapsible):
        """Construit un QGroupBox avec les checkboxes de la section donnée."""
        entries = [e for e in _LAYER_CATALOGUE if e["section"] == section]
        group = QGroupBox(title)
        group_vbox = QVBoxLayout(group)

        if collapsible:
            # Technique collapse : QGroupBox checkable + conteneur masquable.
            # Quand le groupe est décoché, le conteneur est masqué → la boîte
            # se réduit à sa seule barre de titre.
            group.setCheckable(True)
            group.setChecked(False)  # fermé par défaut
            container = QWidget()
            container_vbox = QVBoxLayout(container)
            container_vbox.setContentsMargins(0, 0, 0, 0)
            for entry in entries:
                cb = self._make_checkbox(entry)
                container_vbox.addWidget(cb)
            group_vbox.addWidget(container)
            container.setVisible(False)
            group.toggled.connect(container.setVisible)
        else:
            for entry in entries:
                cb = self._make_checkbox(entry)
                group_vbox.addWidget(cb)

        parent_layout.addWidget(group)

    def _make_checkbox(self, entry):
        cb = QCheckBox(entry["display_name"])
        cb.setChecked(entry["checked"])
        cb.stateChanged.connect(lambda state, e=entry: self._on_check_changed(state, e))
        self._checkboxes[entry["style_key"]] = cb
        return cb

    # ── Gestion de la liste d'ordre ───────────────────────────────────────────

    def _populate_order_list(self):
        """Remplit la liste avec les couches cochées par défaut."""
        for entry in _LAYER_CATALOGUE:
            if entry["checked"]:
                self._add_to_order(entry)

    def _add_to_order(self, entry):
        item = QListWidgetItem(entry["display_name"])
        item.setData(Qt.UserRole, entry["style_key"])
        self._order_list.addItem(item)

    def _remove_from_order(self, style_key):
        for i in range(self._order_list.count()):
            if self._order_list.item(i).data(Qt.UserRole) == style_key:
                self._order_list.takeItem(i)
                return

    def _on_check_changed(self, state, entry):
        if state == Qt.Checked:
            self._add_to_order(entry)
        else:
            self._remove_from_order(entry["style_key"])
            # Effacer l'éditeur si la couche décochée était sélectionnée
            if self._order_list.currentRow() < 0:
                self._clear_style_editor()

    def _move_row(self, delta):
        row = self._order_list.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if new_row < 0 or new_row >= self._order_list.count():
            return
        # Bloquer currentRowChanged pendant le déplacement : takeItem() déclenche
        # le signal avec la mauvaise ligne, ce qui corromprait l'éditeur de style.
        self._order_list.blockSignals(True)
        item = self._order_list.takeItem(row)
        self._order_list.insertItem(new_row, item)
        self._order_list.setCurrentRow(new_row)
        self._order_list.blockSignals(False)
        # Mise à jour manuelle après déplacement complet
        self._on_selection_changed(new_row)

    # ── Éditeur de style ──────────────────────────────────────────────────────

    def _on_selection_changed(self, row):
        if row < 0:
            self._clear_style_editor()
            return
        sk = self._order_list.item(row).data(Qt.UserRole)
        entry = next((e for e in _LAYER_CATALOGUE if e["style_key"] == sk), None)
        if entry:
            self._rebuild_style_editor(entry)

    def _clear_style_editor(self):
        self._swap_style_content(QLabel("Sélectionnez une couche dans la liste."))

    def _rebuild_style_editor(self, entry):
        """Remplace _style_content de façon atomique (replaceWidget) pour éviter
        tout clignotement ou chevauchement entre anciens et nouveaux widgets."""
        sk = entry["style_key"]
        geom_type = entry["geom_type"]

        new_content = QWidget()
        lay = QVBoxLayout(new_content)

        if sk == "sirene":
            lay.addWidget(
                QLabel("Style SIRENE : rendu par règles NAF (non modifiable ici).")
            )
            lay.addStretch()
            self._swap_style_content(new_content)
            return

        # S'assurer que le style existe dans le registre mutable
        if sk not in self._styles:
            default = _DEFAULT_STYLES.get(sk)
            self._styles[sk] = dict(default) if default else {}
        style = self._styles[sk]

        form = QFormLayout()

        if geom_type == "polygon":
            fill_btn = QgsColorButton()
            fill_btn.setAllowOpacity(True)
            fill_btn.setColor(style.get("fill_color", QColor(200, 200, 200, 255)))
            fill_btn.colorChanged.connect(
                lambda col, s=style: s.update({"fill_color": QColor(col)})
            )
            form.addRow("Remplissage :", fill_btn)

            out_col_btn = QgsColorButton()
            out_col_btn.setColor(style.get("outline_color", QColor("#000000")))
            out_col_btn.colorChanged.connect(
                lambda col, s=style: s.update({"outline_color": QColor(col)})
            )
            form.addRow("Contour — couleur :", out_col_btn)

            out_w = QDoubleSpinBox()
            out_w.setRange(0.0, 5.0)
            out_w.setSingleStep(0.1)
            out_w.setDecimals(1)
            out_w.setSuffix(" mm")
            out_w.setValue(style.get("outline_width", 0.3))
            out_w.valueChanged.connect(
                lambda val, s=style: s.update({"outline_width": val})
            )
            form.addRow("Contour — épaisseur :", out_w)

            out_style_combo = QComboBox()
            out_style_combo.addItems(["Plein", "Tirets", "Aucun"])
            out_style_combo.setCurrentIndex(
                {"solid": 0, "dashed": 1, "none": 2}.get(
                    style.get("outline_style", "none"), 2
                )
            )
            out_style_combo.currentIndexChanged.connect(
                lambda idx, s=style: s.update(
                    {"outline_style": ["solid", "dashed", "none"][idx]}
                )
            )
            form.addRow("Contour — style :", out_style_combo)

        elif geom_type == "line":
            line_btn = QgsColorButton()
            line_btn.setColor(style.get("line_color", QColor("#888888")))
            line_btn.colorChanged.connect(
                lambda col, s=style: s.update({"line_color": QColor(col)})
            )
            form.addRow("Couleur :", line_btn)

            lw = QDoubleSpinBox()
            lw.setRange(0.0, 5.0)
            lw.setSingleStep(0.1)
            lw.setDecimals(1)
            lw.setSuffix(" mm")
            lw.setValue(style.get("line_width", 0.5))
            lw.valueChanged.connect(
                lambda val, s=style: s.update({"line_width": val})
            )
            form.addRow("Épaisseur :", lw)

            ls_combo = QComboBox()
            ls_combo.addItems(["Plein", "Tirets"])
            ls_combo.setCurrentIndex(
                {"solid": 0, "dashed": 1}.get(style.get("line_style", "solid"), 0)
            )
            ls_combo.currentIndexChanged.connect(
                lambda idx, s=style: s.update(
                    {"line_style": ["solid", "dashed"][idx]}
                )
            )
            form.addRow("Style :", ls_combo)

        elif geom_type == "point":
            marker_btn = QgsColorButton()
            marker_btn.setColor(style.get("marker_color", QColor("#333333")))
            marker_btn.colorChanged.connect(
                lambda col, s=style: s.update({"marker_color": QColor(col)})
            )
            form.addRow("Couleur :", marker_btn)

            ms = QDoubleSpinBox()
            ms.setRange(0.5, 10.0)
            ms.setSingleStep(0.5)
            ms.setDecimals(1)
            ms.setSuffix(" mm")
            ms.setValue(style.get("marker_size", 2.0))
            ms.valueChanged.connect(
                lambda val, s=style: s.update({"marker_size": val})
            )
            form.addRow("Taille :", ms)

        lay.addLayout(form)
        btn_reset = QPushButton("Réinitialiser")
        btn_reset.clicked.connect(lambda: self._reset_style(entry))
        lay.addWidget(btn_reset)
        lay.addStretch()
        self._swap_style_content(new_content)

    def _swap_style_content(self, new_widget):
        """Remplace _style_content par new_widget de façon synchrone et atomique."""
        self._style_vbox.replaceWidget(self._style_content, new_widget)
        self._style_content.hide()   # masquage immédiat (synchrone)
        self._style_content.deleteLater()
        self._style_content = new_widget

    def _reset_style(self, entry):
        """Remet le style de la couche aux valeurs codées dans _DEFAULT_STYLES."""
        sk = entry["style_key"]
        default = _DEFAULT_STYLES.get(sk)
        if default is not None:
            self._styles[sk] = dict(default)
        self._rebuild_style_editor(entry)

    # ── Validation ────────────────────────────────────────────────────────────

    def accept(self):
        """Collecte l'ordre et les styles puis ferme le dialogue."""
        self.result_layers = []
        for i in range(self._order_list.count()):
            sk = self._order_list.item(i).data(Qt.UserRole)
            entry = next((e for e in _LAYER_CATALOGUE if e["style_key"] == sk), None)
            if entry is None:
                continue
            result_entry = dict(entry)
            result_entry["style"] = self._styles.get(sk)  # None pour SIRENE
            self.result_layers.append(result_entry)
        super().accept()
