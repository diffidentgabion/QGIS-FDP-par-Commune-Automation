# -*- coding: utf-8 -*-
"""
FDP par Commune — Génération automatique d'un fond de plan communal
Script QGIS Processing Toolbox

Installation :
    Traitement > Options > Traitement > Scripts > Dossiers des scripts
    → pointer vers le dossier contenant ce fichier, puis recharger les fournisseurs.
"""

import importlib.util
import json
import os
import tempfile
import time
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
    QgsNetworkAccessManager,
    QgsPointXY,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingParameterString,
    QgsProject,
    QgsProperty,
    QgsRasterLayer,
    QgsRuleBasedRenderer,
    QgsSimpleLineSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsSymbolLayer,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsColorButton
from qgis.PyQt.QtCore import QCoreApplication, QMetaType, Qt
from qgis.PyQt.QtGui import QColor, QPainter
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
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

# =============================================================================
# Chargement des modules helper voisins
# =============================================================================
# Les scripts Processing QGIS n'ont pas de __package__ défini, donc les imports
# relatifs échouent. On charge chaque fichier voisin via importlib avec son
# chemin absolu, ce qui fonctionne quel que soit l'emplacement du script.
def _load_sibling(module_name):
    """Charge et renvoie le module .py voisin `module_name` (sans extension)."""
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), module_name + ".py"
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_sibling("sirene_buildings")
build_activity_layers = _mod.build_activity_layers
SIRENE_CATEGORIES = _mod.SIRENE_CATEGORIES

_mod = _load_sibling("zone_buildings")
build_zone_activity_layers = _mod.build_zone_activity_layers
build_outdoor_space_layers = _mod.build_outdoor_space_layers
_ZB_OUTDOOR_PUBLIC = _mod._OUTDOOR_PUBLIC

_mod = _load_sibling("bati_buildings")
build_bati_layers = _mod.build_bati_layers

_mod = _load_sibling("sirene_display")
build_displaced_sirene_layer = _mod.build_displaced_sirene_layer

ensure_theme_manager = _load_sibling("theme_manager").ensure_theme_manager
del _mod

# =============================================================================
# Catalogue des couches et styles par défaut
# =============================================================================

# Point d'entrée WFS Géoplateforme (IGN). Constante de module pour pouvoir le
# rediriger vers un mandataire local dans les tests.
_WFS_URL = "https://data.geopf.fr/wfs/ows"

# Millésime des couches RPG épinglées à une édition annuelle. La Géoplateforme
# publie chaque année de nouvelles couches RPG dont le typename contient l'année.
# Pour passer à l'édition suivante, ne changer QUE ces deux constantes.
_RPG_YEAR = "2024"
# La couche « Zones densité homogène » ajoute une date de génération dans son nom
# local (ex. surfaces_2024_zdh_20250621) qui change à chaque réédition et n'est
# pas dérivable de l'année seule — à vérifier sur GetCapabilities.
_RPG_ZDH_STAMP = "20250621"

# Ordre du catalogue = ordre initial haut → bas dans la légende QGIS.
# Chaque entry est un dict figé ; le dialogue en fait une copie mutable.
# Natures de zone_de_vegetation retirées au chargement : la BD Forêt V2
# (couche « Végétation haute ») couvre ces formations plus finement
# (les landes via son poste tfv_g11 « Lande »). Bois, Haie, Verger, Vigne restent.
_VEGETATION_NATURES_EXCLUES = frozenset(
    {
        "Forêt fermée de feuillus",
        "Forêt fermée de conifères",
        "Forêt fermée mixte",
        "Forêt ouverte",
        "Peupleraie",
        "Lande ligneuse",
    }
)

# Anciens libellés → libellé actuel. Permet à « Ajout aux communes
# existantes » de reconnaître les couches importées avant le renommage
# et d'éviter de les recharger en doublon sous le nouveau nom.
_LEGACY_DISPLAY_NAMES = {
    "Végétation haute": frozenset({"Forêt (BD Forêt V2)"}),
    "Végétation basse": frozenset({"Végétation"}),
}

_LAYER_CATALOGUE = [
    # ── Zonages de protection (INPN/PatriNat via Géoplateforme) ───────────────
    # Overlays quasi transparents à contour tireté, activés d'un bloc par le
    # toggle « Zonages de protection » du dialogue. En tête de catalogue : le
    # toggle les insère en HAUT de la liste d'ordre → rendus au-dessus de tout.
    # Section prévue pour accueillir d'autres zonages du même type (Géorisques,
    # zonage PLU…).
    {
        "section": "zones",
        "typename": "patrinat_sic:sic",
        "display_name": "Natura 2000 — Habitats (SIC/ZSC)",
        "style_key": "natura_sic",
        "geom_type": "polygon",
        "checked": False,
    },
    {
        "section": "zones",
        "typename": "patrinat_zps:zps",
        "display_name": "Natura 2000 — Oiseaux (ZPS)",
        "style_key": "natura_zps",
        "geom_type": "polygon",
        "checked": False,
    },
    {
        "section": "zones",
        "typename": "patrinat_znieff1:znieff1",
        "display_name": "ZNIEFF type 1",
        "style_key": "znieff1",
        "geom_type": "polygon",
        "checked": False,
    },
    {
        "section": "zones",
        "typename": "patrinat_znieff2:znieff2",
        "display_name": "ZNIEFF type 2",
        "style_key": "znieff2",
        "geom_type": "polygon",
        "checked": False,
    },
    {
        "section": "zones",
        "typename": "patrinat_apb:apb",
        "display_name": "Protection de biotope (APB)",
        "style_key": "apb",
        "geom_type": "polygon",
        "checked": False,
    },
    {
        "section": "zones",
        "typename": "patrinat_pnr:pnr",
        "display_name": "Parc naturel régional",
        "style_key": "pnr",
        "geom_type": "polygon",
        "checked": False,
    },
    # ── Couches par défaut ────────────────────────────────────────────────────
    # Ordre = haut → bas dans la légende (haut = rendu par-dessus)
    {
        "section": "default",
        "typename": None,
        "display_name": "Établissements SIRENE",
        "style_key": "sirene",
        "geom_type": "point",
        "checked": True,
    },
    {
        "section": "default",
        "typename": "ADMINEXPRESS-COG-CARTO.LATEST:commune",
        "display_name": "Commune",
        "style_key": "commune_boundary",
        "geom_type": "polygon",
        "checked": True,
    },
    # Végétation haute (BD Forêt V2) et Végétation basse (BDTOPO) se
    # complètent : la végétation BDTOPO est délestée des natures forestières
    # (voir _VEGETATION_NATURES_EXCLUES) que la BD Forêt couvre plus finement.
    {
        "section": "default",
        "typename": "LANDCOVER.FORESTINVENTORY.V2:formation_vegetale",
        "display_name": "Végétation haute",
        "style_key": "bdforet",
        "geom_type": "polygon",
        "checked": True,
    },
    {
        "section": "default",
        "typename": "BDTOPO_V3:batiment",
        "display_name": "Bâti",
        "style_key": "buildings",
        "geom_type": "polygon",
        "checked": True,
    },
    {
        "section": "default",
        "typename": "BDTOPO_V3:troncon_de_route",
        "display_name": "Voirie",
        "style_key": "roads",
        "geom_type": "line",
        "checked": True,
    },
    {
        "section": "default",
        "typename": "BDTOPO_V3:troncon_de_voie_ferree",
        "display_name": "Voie ferrée",
        "style_key": "railways",
        "geom_type": "line",
        "checked": True,
    },
    {
        "section": "default",
        "typename": "BDTOPO_V3:zone_de_vegetation",
        "display_name": "Végétation basse",
        "style_key": "vegetation",
        "geom_type": "polygon",
        "checked": True,
    },
    {
        "section": "default",
        "typename": "BDTOPO_V3:equipement_de_transport",
        "display_name": "Équipements de transport",
        "style_key": "equipement_de_transport",
        "geom_type": "polygon",
        "checked": True,
    },
    {
        "section": "default",
        "typename": "BDTOPO_V3:piste_d_aerodrome",
        "display_name": "Piste d'aérodrome",
        "style_key": "piste_d_aerodrome",
        "geom_type": "polygon",
        "checked": True,
    },
    {
        "section": "default",
        "typename": "BDTOPO_V3:aerodrome",
        "display_name": "Aérodrome",
        "style_key": "aerodrome",
        "geom_type": "polygon",
        "checked": True,
    },
    {
        "section": "default",
        "typename": "BDTOPO_V3:surface_hydrographique",
        "display_name": "Hydrographie - surface",
        "style_key": "water_surface",
        "geom_type": "polygon",
        "checked": True,
    },
    {
        "section": "default",
        "typename": "BDTOPO_V3:cours_d_eau",
        "display_name": "Hydrographie - cours d'eau",
        "style_key": "rivers",
        "geom_type": "line",
        "checked": True,
    },
    {
        "section": "default",
        "typename": "BDTOPO_V3:reservoir",
        "display_name": "Réservoir",
        "style_key": "reservoir",
        "geom_type": "polygon",
        "checked": True,
    },
    {
        "section": "default",
        "typename": "BDTOPO_V3:zone_d_activite_ou_d_interet",
        "display_name": "Zones d'activité et d'intérêt",
        "style_key": "zai",
        "geom_type": "polygon",
        "checked": True,
    },
    {
        "section": "default",
        "typename": "BDTOPO_V3:terrain_de_sport",
        "display_name": "Terrain de sport",
        "style_key": "terrain_de_sport",
        "geom_type": "polygon",
        "checked": True,
    },
    {
        "section": "default",
        "typename": "BDTOPO_V3:cimetiere",
        "display_name": "Cimetières",
        "style_key": "cimetiere",
        "geom_type": "polygon",
        "checked": True,
    },
    # ── Couches rurales RPG (désactivées par défaut) ──────────────────────────
    # RPG.LATEST:parcelles_graphiques et RPG.LATEST:ilots_anonymes ont un alias
    # LATEST stable. Les quatre suivantes sont épinglées à une année : leurs
    # typenames dérivent de _RPG_YEAR (défini en haut du fichier) — changer cette
    # seule constante pour passer à l'édition suivante.
    # Haie en premier : ajouté en tête du groupe Agriculture dans la légende.
    {
        "section": "rural",
        "typename": "BDTOPO_V3:haie",
        "display_name": "Haies",
        "style_key": "haie",
        "geom_type": "line",
        "checked": False,
    },
    {
        "section": "rural",
        "typename": "RPG.LATEST:parcelles_graphiques",
        "display_name": "Parcelles agricoles",
        "style_key": "rpg_parcelles",
        "geom_type": "polygon",
        "checked": False,
    },
    {
        "section": "rural",
        "typename": "RPG.LATEST:ilots_anonymes",
        "display_name": "Îlots",
        "style_key": "rpg_ilots",
        "geom_type": "polygon",
        "checked": False,
    },
    {
        "section": "rural",  # millésime dérivé de _RPG_YEAR
        "typename": f"IGNF_RPG_PARCELLES-AGRICOLES-CATEGORISEES_{_RPG_YEAR}:parcelles_agricole_categorisees_{_RPG_YEAR}",
        "display_name": "Catégories PAC",
        "style_key": "rpg_pac",
        "geom_type": "polygon",
        "checked": False,
    },
    {
        "section": "rural",  # millésime dérivé de _RPG_YEAR
        "typename": f"IGNF_RPG_PRAIRIES-PERMANENTES_{_RPG_YEAR}:prairies_permanentes_{_RPG_YEAR}",
        "display_name": "Prairies permanentes",
        "style_key": "rpg_pp",
        "geom_type": "polygon",
        "checked": False,
    },
    {
        "section": "rural",  # millésime dérivé de _RPG_YEAR
        "typename": f"IGNF_RPG_PARCELLES-ELIGIBLES-IAE:parcelles_eligibles_iae_{_RPG_YEAR}",
        "display_name": "Infra. agro-env.",
        "style_key": "rpg_iae",
        "geom_type": "polygon",
        "checked": False,
    },
    {
        "section": "rural",  # millésime dérivé de _RPG_YEAR + _RPG_ZDH_STAMP
        "typename": f"IGNF_RPG_ZONES-DENSITE-HOMOGENE_{_RPG_YEAR}:surfaces_{_RPG_YEAR}_zdh_{_RPG_ZDH_STAMP}",
        "display_name": "Zones densité homogène",
        "style_key": "rpg_zdh",
        "geom_type": "polygon",
        "checked": False,
    },
    # ── Couches supplémentaires ───────────────────────────────────────────────
    {
        "section": "default",
        "typename": "BDTOPO_V3:construction_surfacique",
        "display_name": "Constructions surfaciques",
        "style_key": "construction_surfacique",
        "geom_type": "polygon",
        "checked": True,
    },
    {
        "section": "extra",
        "typename": "BDTOPO_V3:itineraire_autre",
        "display_name": "Itinéraires (vélo, pédestre)",
        "style_key": "itineraire_autre",
        "geom_type": "line",
        "checked": False,
    },
    {
        "section": "extra",
        "typename": "BDTOPO_V3:detail_hydrographique",
        "display_name": "Détails hydrographiques",
        "style_key": "detail_hydrographique",
        "geom_type": "point",
        "checked": False,
    },
    {
        "section": "extra",
        "typename": "BDTOPO_V3:canalisation",
        "display_name": "Canalisation",
        "style_key": "canalisation",
        "geom_type": "line",
        "checked": False,
    },
    {
        "section": "extra",
        "typename": "BDTOPO_V3:construction_lineaire",
        "display_name": "Construction linéaire",
        "style_key": "construction_lineaire",
        "geom_type": "line",
        "checked": False,
    },
    {
        "section": "extra",
        "typename": "BDTOPO_V3:construction_ponctuelle",
        "display_name": "Construction ponctuelle",
        "style_key": "construction_ponctuelle",
        "geom_type": "point",
        "checked": False,
    },
    {
        "section": "extra",
        "typename": "BDTOPO_V3:detail_orographique",
        "display_name": "Détail orographique",
        "style_key": "detail_orographique",
        "geom_type": "point",
        "checked": False,
    },
    {
        "section": "extra",
        "typename": "BDTOPO_V3:lieu_dit_non_habite",
        "display_name": "Lieu-dit non habité",
        "style_key": "lieu_dit_non_habite",
        "geom_type": "point",
        "checked": False,
    },
    {
        "section": "extra",
        "typename": "BDTOPO_V3:pylone",
        "display_name": "Pylône",
        "style_key": "pylone",
        "geom_type": "point",
        "checked": False,
    },
    # ── Fond de référence ─────────────────────────────────────────────────────
    {
        "section": "default",
        "typename": "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle",
        "display_name": "Parcelles cadastrales",
        "style_key": "parcels",
        "geom_type": "polygon",
        "checked": True,
    },
    {
        "section": "default",
        "typename": None,
        "display_name": "OpenStreetMap",
        "style_key": "osm",
        "geom_type": "raster",
        "checked": False,
    },
]

# Styles par défaut : style_key → dict de valeurs prêtes à l'emploi.
# QColor avec canal alpha pour l'opacité du remplissage.
# Clé "sirene" → None (rendu règle-par-règle, non éditable ici).
_DEFAULT_STYLES = {
    # ── Couches par défaut ────────────────────────────────────────────────────
    "sirene": None,
    "buildings": {
        "geom_type": "polygon",
        "fill_color": QColor(162, 160, 178, 255),
        "outline_color": QColor("#9898aa"),
        "outline_width": 0.1,
        "outline_style": "none",
    },
    "roads": None,  # rendu règle-par-règle (QgsRuleBasedRenderer), non éditable ici
    "railways": None,  # rendu règle-par-règle (QgsRuleBasedRenderer), non éditable ici
    "aerodrome": {
        "geom_type": "polygon",
        "fill_color": QColor(200, 205, 185, 255),
        "outline_color": QColor("#8A9070"),
        "outline_width": 0.3,
        "outline_style": "none",
    },
    "piste_d_aerodrome": {
        "geom_type": "polygon",
        "fill_color": QColor(110, 120, 100, 255),
        "outline_color": QColor("#505840"),
        "outline_width": 0.2,
        "outline_style": "none",
    },
    "vegetation": None,  # rendu règle-par-règle via _apply_vegetation_style (nature)
    "rivers": None,  # rendu règle-par-règle via _apply_rivers_style (caractere_permanent)
    "water_surface": None,  # rendu règle-par-règle via _apply_water_surface_style (nature + persistance)
    "parcels": {
        "geom_type": "polygon",
        "fill_color": QColor(224, 224, 224, 255),
        "outline_color": QColor("#ffffff"),
        "outline_width": 0.2,
        "outline_style": "solid",
    },
    "commune_boundary": {
        "geom_type": "polygon",
        "fill_color": QColor(0, 0, 0, 0),
        "outline_color": QColor("#000000"),
        "outline_width": 0.5,
        "outline_style": "solid",
    },
    "reservoir": {
        "geom_type": "polygon",
        "fill_color": QColor(80, 200, 230, 220),
        "outline_color": QColor("#3A9BD5"),
        "outline_width": 0.3,
        "outline_style": "solid",
    },
    # ── Couches supplémentaires ───────────────────────────────────────────────
    "construction_surfacique": None,  # rendu par sublayers via build_construction_surfacique_layers
    "itineraire_autre": {
        "geom_type": "line",
        "line_color": QColor("#15A87C"),
        "line_width": 0.4,
        "line_style": "solid",
    },
    "haie": {
        "geom_type": "line",
        "line_color": QColor("#29A86A"),
        "line_width": 0.6,
        "line_style": "solid",
    },
    "cimetiere": {
        "geom_type": "polygon",
        "fill_color": QColor(185, 170, 148, 255),
        "outline_color": QColor("#9a8a78"),
        "outline_width": 0.3,
        "outline_style": "none",
    },
    "detail_hydrographique": {
        "geom_type": "point",
        "marker_color": QColor("#3A9BD5"),
        "marker_size": 2.0,
    },
    "bdforet": None,  # rendu règle-par-règle via _apply_bdforet_style (tfv_g11)
    # ── Zonages environnementaux : remplissage quasi transparent, contour tireté
    "natura_sic": {
        "geom_type": "polygon",
        "fill_color": QColor(46, 139, 87, 40),
        "outline_color": QColor("#2E8B57"),
        "outline_width": 0.5,
        "outline_style": "dashed",
    },
    "natura_zps": {
        "geom_type": "polygon",
        "fill_color": QColor(70, 130, 180, 40),
        "outline_color": QColor("#4682B4"),
        "outline_width": 0.5,
        "outline_style": "dashed",
    },
    "znieff1": {
        "geom_type": "polygon",
        "fill_color": QColor(184, 134, 11, 40),
        "outline_color": QColor("#B8860B"),
        "outline_width": 0.4,
        "outline_style": "dashed",
    },
    "znieff2": {
        "geom_type": "polygon",
        "fill_color": QColor(218, 165, 32, 25),
        "outline_color": QColor("#DAA520"),
        "outline_width": 0.4,
        "outline_style": "dashed",
    },
    "apb": {
        "geom_type": "polygon",
        "fill_color": QColor(160, 80, 140, 40),
        "outline_color": QColor("#A0508C"),
        "outline_width": 0.4,
        "outline_style": "dashed",
    },
    # PNR : périmètres immenses — contour seul, aucun remplissage
    "pnr": {
        "geom_type": "polygon",
        "fill_color": QColor(0, 0, 0, 0),
        "outline_color": QColor("#2F4F4F"),
        "outline_width": 0.6,
        "outline_style": "dashed",
    },
    # ── Couches supplémentaires avancées ──────────────────────────────────────
    "canalisation": {
        "geom_type": "line",
        "line_color": QColor("#29ABE2"),
        "line_width": 0.5,
        "line_style": "solid",
    },
    "construction_lineaire": {
        "geom_type": "line",
        "line_color": QColor("#7878A0"),
        "line_width": 0.5,
        "line_style": "solid",
    },
    "construction_ponctuelle": {
        "geom_type": "point",
        "marker_color": QColor("#7878A0"),
        "marker_size": 2.0,
    },
    "detail_orographique": {
        "geom_type": "point",
        "marker_color": QColor("#C24C6A"),
        "marker_size": 2.0,
    },
    "lieu_dit_non_habite": {
        "geom_type": "point",
        "marker_color": QColor("#555588"),
        "marker_size": 2.0,
    },
    "pylone": {
        "geom_type": "point",
        "marker_color": QColor("#8888AA"),
        "marker_size": 2.0,
    },
    "terrain_de_sport": {
        "geom_type": "polygon",
        "fill_color": QColor(253, 185, 122, 255),
        "outline_color": QColor("#aaaaaa"),
        "outline_width": 0.2,
        "outline_style": "none",
    },
    "zai": None,  # rendu règle-par-règle via _apply_zai_style, non éditable dans le dialogue
    "equipement_de_transport": None,  # rendu par sublayers via build_transport_layers, non éditable dans le dialogue
    # ── Couches rurales RPG ───────────────────────────────────────────────────
    "rpg_parcelles": None,  # rendu règle-par-règle via _apply_rpg_parcelles_style
    "rpg_ilots": None,  # rendu symbole unique via _apply_rpg_ilots_style
    "rpg_pac": None,  # rendu règle-par-règle via _apply_rpg_pac_style
    "rpg_pp": None,  # rendu symbole unique via _apply_rpg_pp_style
    "rpg_iae": None,  # rendu règle-par-règle via _apply_rpg_iae_style
    "rpg_zdh": None,  # rendu règle-par-règle via _apply_rpg_zdh_style
}


# =============================================================================
# Équipements de transport — couleurs par label (natd || nat)
# =============================================================================
# Valeurs observées sur le WFS Géoplateforme (échantillon 500 entités, 184 707 total).

_TRANSPORT_COLORS = {
    # ── Routes & intersections (bleu-ardoise vif) ────────────────────────────
    "Péage": "#90B8D8",  # bleu clair — péage visible
    "Carrefour": "#6090C0",  # bleu moyen-clair
    "Rond-point": "#3870A8",  # bleu moyen
    "Echangeur partiel": "#2058A0",  # bleu moyen-foncé
    "Echangeur": "#103878",  # bleu foncé
    # ── Ferroviaire & urbain (rouges/oranges + violet pour transit urbain) ────
    "Station de tramway": "#AB47BC",  # violet moyen — tramway
    "Station de métro": "#7B1FA2",  # violet foncé — métro
    "Gare routière": "#FFA000",  # ambre vif — autocar
    "Gare RER": "#E53935",  # rouge vif — RER
    "Gare voyageurs uniquement": "#C62828",  # rouge foncé — grande gare
    "Gare voyageurs et fret": "#E64A19",  # orange-rouge — mixte
    "Gare fret uniquement": "#BF360C",  # orange brûlé foncé — fret
    "Aire de triage": "#4030a0",  # Indigo-violet moyen — infra voies
    # ── Ports (bleus vifs) ───────────────────────────────────────────────────
    "Port de plaisance": "#29B6F6",  # bleu ciel vif — loisir
    "Port": "#1565C0",  # bleu marine moyen
    "Port de commerce": "#0D47A1",  # bleu marine foncé — commerce
    # ── Stationnement ────────────────────────────────────────────────────────
    "Parking": "#5C6BC0",  # indigo — distinct des autres groupes
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
        nat_str = (feat["nature"] or "").strip()
        natd_str = (feat["nature_detaillee"] or "").strip()
        if nat_str == "NULL":
            nat_str = ""
        if natd_str == "NULL":
            natd_str = ""
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

    known = [l for l in _TRANSPORT_COLORS if l in label_fids]
    unknown = sorted(l for l in label_fids if l not in _TRANSPORT_COLORS)

    for label in known + unknown:
        fids = label_fids[label]
        color_hex = _TRANSPORT_COLORS.get(label, "#AAAAAA")

        mem_layer = QgsVectorLayer(f"Polygon?crs={crs_id}", label, "memory")
        pr = mem_layer.dataProvider()
        pr.addAttributes(fields.toList())
        mem_layer.updateFields()
        pr.addFeatures(
            list(equip_layer.getFeatures(QgsFeatureRequest().setFilterFids(fids)))
        )
        mem_layer.updateExtents()

        c = QColor(color_hex)
        sym = QgsFillSymbol.createSimple(
            {
                "color": f"{c.red()},{c.green()},{c.blue()},{c.alpha()}",
                "outline_style": "no",
            }
        )
        mem_layer.setRenderer(QgsSingleSymbolRenderer(sym))
        results.append(mem_layer)

    return results


# =============================================================================
# Constructions surfaciques — catégorisation par nature
# =============================================================================
# Natures routées vers le groupe Équipements de transport (infrastructures de franchissement).
_PONT_NATURES = {"Pont", "Viaduc", "Ponceau"}

_CONSTRUCTION_SURFACIQUE_COLORS = {
    # ── Franchissements (→ groupe transport) ─────────────────────────────────
    "Pont": "#FFFFFF",  # blanc pur — tablier de pont
    "Viaduc": "#E8E0D0",  # beige chaud — viaduc maçonné
    "Ponceau": "#F0EEE8",  # blanc cassé — petit franchissement
    # ── Ouvrages hydrauliques ──────────────────────────────────────────────
    "Barrage": "#2E86C1",  # bleu vif — retenue d'eau
    "Digue": "#C4A35A",  # ocre doré — remblai terreux
    "Ecluse": "#14A691",  # sarcelle — navigation fluviale
    # ── Ouvrages de génie civil ───────────────────────────────────────────
    "Remblai": "#BC7A40",  # brun chaud — terrassement
    "Talus": "#9A7050",  # brun moyen — pente aménagée
    "Tunnel": "#505060",  # anthracite — ouvrage souterrain
    # ── Éléments bâtis courants ───────────────────────────────────────────
    "Mur": "#909090",  # gris moyen — mur de clôture/soutènement
    "Escalier": "#BFB0A0",  # pierre chaude — escalier extérieur
    "Passage à niveau": "#F4C430",  # ambre vif — signalement sécurité
}


def build_construction_surfacique_layers(constr_layer, feedback) -> list:
    """
    Génère des couches mémoire pour construction_surfacique, séparées par nature.

    Retourne list[QgsVectorLayer] ordonnées par _CONSTRUCTION_SURFACIQUE_COLORS
    puis alphabétiquement pour les natures inconnues.
    L'appelant est responsable du routage : les couches dont le nom est dans
    _PONT_NATURES vont dans le groupe Équipements de transport, les autres
    dans un groupe Constructions surfaciques.
    """
    label_fids = {}
    for processed, feat in enumerate(constr_layer.getFeatures()):
        if processed % 500 == 0 and feedback.isCanceled():
            return []
        nat_str = (feat["nature"] or "").strip()
        if nat_str == "NULL":
            nat_str = ""
        if not nat_str:
            continue
        label_fids.setdefault(nat_str, []).append(feat.id())

    if not label_fids:
        return []

    crs_id = constr_layer.crs().authid()
    fields = constr_layer.fields()
    results = []

    known = [l for l in _CONSTRUCTION_SURFACIQUE_COLORS if l in label_fids]
    unknown = sorted(l for l in label_fids if l not in _CONSTRUCTION_SURFACIQUE_COLORS)

    for label in known + unknown:
        fids = label_fids[label]
        color_hex = _CONSTRUCTION_SURFACIQUE_COLORS.get(label, "#AAAAAA")

        mem_layer = QgsVectorLayer(f"Polygon?crs={crs_id}", label, "memory")
        pr = mem_layer.dataProvider()
        pr.addAttributes(fields.toList())
        mem_layer.updateFields()
        pr.addFeatures(
            list(constr_layer.getFeatures(QgsFeatureRequest().setFilterFids(fids)))
        )
        mem_layer.updateExtents()

        c = QColor(color_hex)
        outline = "no" if label not in _PONT_NATURES else "solid"
        sym = QgsFillSymbol.createSimple(
            {
                "color": f"{c.red()},{c.green()},{c.blue()},{c.alpha()}",
                "outline_style": outline,
                "outline_color": "#aaaaaa",
                "outline_width": "0.3",
            }
        )
        mem_layer.setRenderer(QgsSingleSymbolRenderer(sym))
        results.append(mem_layer)

    return results


# =============================================================================
# Helpers renderer partagés
# =============================================================================


def _make_line_rule(label, expr, color, width_mm, pen_style=Qt.SolidLine):
    """
    Crée un QgsRuleBasedRenderer.Rule avec un QgsSimpleLineSymbolLayer.
    Partagé par _apply_roads_style, _apply_railways_style et
    _apply_courbe_de_niveau_style pour éviter la duplication de ce boilerplate.
    """
    sl = QgsSimpleLineSymbolLayer()
    sl.setColor(QColor(color))
    sl.setWidth(width_mm)
    sl.setPenStyle(pen_style)
    sym = QgsLineSymbol()
    sym.deleteSymbolLayer(0)
    sym.appendSymbolLayer(sl)
    rule = QgsRuleBasedRenderer.Rule(sym)
    rule.setLabel(label)
    rule.setFilterExpression(expr)
    return rule


def _apply_fill_rules(layer, rules, fallback_color, fallback_label, symbol_props=None):
    """
    Applique à `layer` un rendu règle-par-règle de remplissages + un repli ELSE.

    `rules`        : itérable de (label, color, expr).
    `symbol_props` : propriétés QgsFillSymbol communes à toutes les règles
                     (contour…), fusionnées avec la couleur de chaque règle.

    Factorise le boilerplate partagé par _apply_zai_style et les styles RPG
    polygonaux. Le rendu produit est identique au code inline qu'il remplace.
    """
    base = dict(symbol_props or {})
    root = QgsRuleBasedRenderer.Rule(None)
    for label, color, expr in rules:
        rule = QgsRuleBasedRenderer.Rule(
            QgsFillSymbol.createSimple({**base, "color": color})
        )
        rule.setFilterExpression(expr)
        rule.setLabel(label)
        root.appendChild(rule)
    fallback = QgsRuleBasedRenderer.Rule(
        QgsFillSymbol.createSimple({**base, "color": fallback_color})
    )
    fallback.setFilterExpression("ELSE")
    fallback.setLabel(fallback_label)
    root.appendChild(fallback)
    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.triggerRepaint()


def _apply_line_rules(layer, rules):
    """
    Applique à `layer` un rendu règle-par-règle de lignes.

    `rules` : itérable de (label, expr, color, width_mm[, pen_style]) — passé tel
    quel à _make_line_rule (pen_style optionnel → trait plein par défaut).

    Factorise le boilerplate partagé par _apply_roads_style, _apply_railways_style
    et _apply_courbe_de_niveau_style. Rendu identique au code inline remplacé.
    """
    root = QgsRuleBasedRenderer.Rule(None)
    for rule_args in rules:
        root.appendChild(_make_line_rule(*rule_args))
    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.triggerRepaint()


# =============================================================================
# Données RPG — groupes de cultures (séparées du code de rendu)
# =============================================================================

_RPG_PARCELLES_GROUPS = [
    # ── Céréales ──────────────────────────────────────────────────────
    (
        "Céréales",
        (
            "AVH",
            "AVP",
            "BDH",
            "BDP",
            "BTH",
            "BTP",
            "CAG",
            "CAH",
            "EPE",
            "MCS",
            "MCR",
            "MID",
            "MIS",
            "MLT",
            "MOH",
            "ORH",
            "ORP",
            "RIZ",
            "SGH",
            "SGP",
            "SOG",
            "SRS",
            "TTH",
            "TTP",
        ),
        "#F0D060",
    ),
    # ── Oléagineux & protéagineux ──────────────────────────────────────
    (
        "Oléagineux & protéagineux",
        (
            "ARA",
            "CML",
            "CZH",
            "CZP",
            "FEV",
            "FVL",
            "FVP",
            "GES",
            "LDH",
            "LDP",
            "LEC",
            "LIH",
            "LIP",
            "MOT",
            "MPC",
            "OAG",
            "OEI",
            "OHR",
            "PAG",
            "PCH",
            "PHI",
            "PHS",
            "PPR",
            "SOJ",
            "TRN",
        ),
        "#E8A800",
    ),
    # ── Prairies permanentes ───────────────────────────────────────────
    ("Prairies permanentes", ("PPH", "SPH", "SPL"), "#18A018"),
    # ── Prairies & fourrages temporaires ──────────────────────────────
    (
        "Prairies & fourrages temporaires",
        (
            "AFG",
            "CPL",
            "GRA",
            "LOT",
            "LUZ",
            "MLC",
            "MLF",
            "MLG",
            "PTR",
            "SAI",
            "TRE",
            "VES",
        ),
        "#70DC70",
    ),
    # ── Vignes ────────────────────────────────────────────────────────
    ("Vignes", ("VRC",), "#8B1A2A"),
    # ── Arboriculture & vergers ────────────────────────────────────────
    (
        "Arboriculture & vergers",
        (
            "ACP",
            "AGR",
            "CBT",
            "CTG",
            "FLP",
            "NOS",
            "NOX",
            "OLI",
            "PRU",
            "PVT",
            "PWT",
            "TRU",
            "VRG",
        ),
        "#FF8C00",
    ),
    # ── Maraîchage & légumes ───────────────────────────────────────────
    (
        "Maraîchage & légumes",
        (
            "AIL",
            "ART",
            "CAR",
            "CCN",
            "CEL",
            "CHU",
            "EPI",
            "FLA",
            "FRA",
            "LBF",
            "MDI",
            "MLO",
            "NVT",
            "OIG",
            "PFR",
            "PHF",
            "POR",
            "POT",
            "PSL",
            "PTC",
            "PVP",
            "RDI",
            "TOM",
        ),
        "#70EC70",
    ),
    # ── Jachères & surfaces temporairement non exploitées ─────────────
    ("Jachères & sol nu", ("JAC", "JNO", "SNE"), "#D4A060"),
    # ── Cultures industrielles & énergie ──────────────────────────────
    (
        "Cultures industrielles & énergie",
        ("BTN", "CHV", "CSE", "HBL", "LIF", "MSW", "TAB", "TCR"),
        "#E07820",
    ),
    # ── PPAM — Plantes à Parfum, Aromatiques et Médicinales ───────────
    (
        "PPAM — Aromatiques & médicinales",
        ("AAR", "AME", "ARP", "FNU", "LAV", "PME", "PPP", "PRF"),
        "#CC60CC",
    ),
    # ── Horticulture & pépinières ──────────────────────────────────────
    ("Horticulture & pépinières", ("CSS", "HPC", "PEP", "PEV"), "#90D890"),
    # ── Surfaces boisées & sylvopastorale ─────────────────────────────
    ("Surfaces boisées", ("CAE", "CEE", "CNA", "CNE", "SBO"), "#207030"),
    # ── Cultures tropicales (DOM) ──────────────────────────────────────
    (
        "Cultures tropicales",
        ("ANA", "BCA", "BEF", "CAC", "CSA", "SHD", "TBT", "VNL"),
        "#F4A04E",
    ),
    # ── Surfaces environnementales ─────────────────────────────────────
    (
        "Surfaces environnementales",
        ("BFS", "BOR", "BTA", "MRS", "SAG", "SIN", "SNU"),
        "#88C099",
    ),
    # ── Mélanges complexes (interrangs) ───────────────────────────────
    ("Mélanges complexes", ("CID", "CIT"), "#C8B830"),
]


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
            "Génère un fond de plan communal vectoriel à partir des données ouvertes :\n"
            "  • IGN BD TOPO / ADMIN EXPRESS / Cadastre (WFS Géoplateforme)\n"
            "  • Établissements économiques (Géo-SIRENE)\n\n"
            "Saisissez le nom de la commune, en entier ou en partie.\n"
            "Un dialogue permet ensuite de choisir les couches et de les ordonner."
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
        feedback.pushInfo(f"🔍  Recherche de « {nom_input} »…")

        commune = self._search_commune(nom_input)
        if commune is None:
            raise Exception("Aucune commune sélectionnée. Traitement annulé.")

        nom = commune["nom"]
        insee = commune["code"]
        dep = self._get_dep(insee)
        feedback.pushInfo(f"📍  {nom} ({dep}) — INSEE {insee}")
        feedback.setProgress(5)

        # ── 2. Sélection des couches et édition des styles ────────────────────
        dlg_sel = _LayerSelectorDialog()
        if dlg_sel.exec_() != QDialog.Accepted:
            raise Exception("Sélection des couches annulée.")
        selected_entries = dlg_sel.result_layers
        if not selected_entries and dlg_sel.topo_config is None:
            raise Exception("Aucune couche sélectionnée.")

        self._run_commune_import(
            commune, selected_entries, dlg_sel.topo_config, feedback
        )
        return {}

    # =========================================================================
    # Logique d'import commune — réutilisée par FDPImportEnLot (import en lot)
    # =========================================================================

    def _run_commune_import(
        self, commune, selected_entries, topo_config, feedback, show_save_dialog=True
    ):
        nom = commune["nom"]
        insee = commune["code"]

        # ── Géométrie communale reprojetée en EPSG:2154 ──────────────────────
        crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
        crs_2154 = QgsCoordinateReferenceSystem("EPSG:2154")
        xform = QgsCoordinateTransform(crs_4326, crs_2154, QgsProject.instance())

        # Le contour renvoyé par l'API Géo est en GeoJSON / EPSG:4326
        commune_geom = self._geojson_to_qgsgeometry(commune["geometry"])
        commune_geom.transform(xform)
        bbox = commune_geom.boundingBox()

        # Couche limite unique réutilisée pour tous les découpages
        boundary_layer = self._geom_to_temp_layer(commune_geom, "Polygon", crs_2154)
        feedback.setProgress(10)

        # ── Chargement des couches WFS ────────────────────────────────────────
        loaded_layers = {}  # style_key → QgsVectorLayer
        wfs_entries = [e for e in selected_entries if e["typename"] is not None]
        sirene_entry = next(
            (e for e in selected_entries if e["style_key"] == "sirene"), None
        )
        total_layers = len(wfs_entries) + (1 if sirene_entry else 0)
        progress_per_layer = 40 / max(total_layers, 1)

        feedback.pushInfo(f"⬇  Téléchargement des couches ({total_layers})…")
        for i, entry in enumerate(wfs_entries):
            if feedback.isCanceled():
                return
            feedback.pushInfo(f"   {entry['display_name']}…")
            if entry["style_key"] == "parcels":
                # Parcelles : filtre code_insee + matérialisation sans découpage
                # (elles épousent déjà la limite communale) — bien plus rapide.
                layer = self._load_parcelle_layer(
                    entry["typename"], entry["display_name"], insee,
                    bbox, boundary_layer, crs_2154, feedback,
                )
            else:
                layer = self._load_wfs_layer(
                    entry["typename"],
                    entry["display_name"],
                    bbox,
                    boundary_layer,
                    crs_2154,
                    feedback,
                )
            if layer:
                loaded_layers[entry["style_key"]] = layer
            feedback.setProgress(10 + int((i + 1) * progress_per_layer))

        # ── 4. Établissements SIRENE ──────────────────────────────────────────
        if sirene_entry and not feedback.isCanceled():
            feedback.pushInfo("⬇  Établissements économiques…")
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
            feedback.pushInfo("   📌  Placement des établissements…")
            loaded_layers["sirene"] = build_displaced_sirene_layer(
                loaded_layers["sirene"],
                loaded_layers["buildings"],
                feedback,
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
            feedback.pushInfo("   🏗  Catégories d'activité…")
            activity_layers = build_activity_layers(
                loaded_layers["buildings"], loaded_layers["sirene"], feedback
            )
            feedback.pushInfo(f"   ✓  {len(activity_layers)} catégorie(s)")

        # ── 4c. Couches bâtiments colorées par zone d'activité (ZAI) ─────────
        zai_layer = loaded_layers.get("zai")
        zone_layers = []
        if not feedback.isCanceled() and zai_layer and "buildings" in loaded_layers:
            feedback.pushInfo("   🏭  Zones d'activité…")
            zone_layers = build_zone_activity_layers(
                loaded_layers["buildings"], zai_layer, feedback
            )
            n_cat = len({lbl for lbl, _ in zone_layers})
            feedback.pushInfo(
                f"   ✓  {len(zone_layers)} couche(s), {n_cat} catégorie(s)"
            )

        # ── 4c-bis. Espaces publics extérieurs (parcs, places, squares…) ───────
        outdoor_layers = []
        if zai_layer and not feedback.isCanceled():
            feedback.pushInfo("   🌳  Espaces publics…")
            outdoor_layers = build_outdoor_space_layers(zai_layer, feedback)
            feedback.pushInfo(f"   ✓  {len(outdoor_layers)} espace(s)")
            # Supprimer ces entités de la couche ZAI de base pour éviter le doublon.
            # Mêmme logique de label que dans zone_buildings.py (natd || nat).
            if outdoor_layers:
                ids_outdoor = []
                for feat in zai_layer.getFeatures():
                    natd = str(feat["nature_detaillee"] or "").strip()
                    nat = str(feat["nature"] or "").strip()
                    if natd == "NULL":
                        natd = ""
                    if nat == "NULL":
                        nat = ""
                    label = natd if natd else nat
                    if label in _ZB_OUTDOOR_PUBLIC:
                        ids_outdoor.append(feat.id())
                if ids_outdoor:
                    zai_layer.dataProvider().deleteFeatures(ids_outdoor)

        # ── 4d. Couches équipements de transport colorées par type ───────────
        transport_layers = []
        equip_layer = loaded_layers.get("equipement_de_transport")
        if equip_layer and not feedback.isCanceled():
            feedback.pushInfo("   🚉  Équipements de transport…")
            transport_layers = build_transport_layers(equip_layer, feedback)
            feedback.pushInfo(f"   ✓  {len(transport_layers)} type(s)")

        # ── 4e. Fond OSM optionnel ────────────────────────────────────────────
        osm_entry = next((e for e in selected_entries if e["style_key"] == "osm"), None)
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
        bati_stats = []
        bati_layers = []
        if "buildings" in loaded_layers and not feedback.isCanceled():
            feedback.pushInfo("   🏘  Analyse du bâti…")
            bati_stats, bati_layers = build_bati_layers(
                loaded_layers["buildings"], feedback
            )
            feedback.pushInfo(f"   ✓  {len(bati_layers)} couche(s)")

        # ── 4g. Topographie ───────────────────────────────────────────────────
        topo_layer = None
        hillshade_layer = None
        if topo_config and not feedback.isCanceled():
            feedback.pushInfo("⛰  Topographie…")
            if topo_config["mode"] == "wfs":
                topo_layer = self._load_topo_wfs(
                    bbox, boundary_layer, crs_2154, feedback
                )
            else:
                topo_layer, hillshade_layer = self._load_topo_lidar(
                    bbox,
                    boundary_layer,
                    crs_2154,
                    topo_config["interval"],
                    topo_config.get("z_factor", 1.5),
                    topo_config.get("azimuth", 315),
                    feedback,
                )
            if topo_layer:
                self._apply_courbe_de_niveau_style(topo_layer)
                feedback.pushInfo(f"   ✓  {topo_layer.featureCount()} courbe(s)")

        # ── 5. Groupe QGIS + symbologie + ajout des couches ──────────────────
        feedback.pushInfo("🗺  Assemblage du projet…")
        # L'ordre du dialogue est haut → bas dans la légende.
        # group.addLayer() ajoute en fin de liste enfants, donc le premier entry
        # se retrouve à l'index 0 (sommet = rendu par-dessus).
        root = QgsProject.instance().layerTreeRoot()
        group = root.insertGroup(0, nom)
        group.setCustomProperty("fdp_insee", insee)

        # Topographie : ajoutée en premier enfant → position 0 = sommet de la légende.
        # Ordre dans le groupe : courbes au-dessus, hillshade en-dessous (Superposition).
        if topo_layer or hillshade_layer:
            topo_grp = group.addGroup("Topographie")
            if topo_layer:
                QgsProject.instance().addMapLayer(topo_layer, False)
                topo_grp.addLayer(topo_layer)
            if hillshade_layer:
                hillshade_layer.setBlendMode(QPainter.CompositionMode_Overlay)
                hillshade_layer.brightnessFilter().setBrightness(-50)
                QgsProject.instance().addMapLayer(hillshade_layer, False)
                topo_grp.addLayer(hillshade_layer)

        _RPG_KEYS = {
            "rpg_parcelles",
            "rpg_ilots",
            "rpg_pac",
            "rpg_pp",
            "rpg_iae",
            "rpg_zdh",
            "haie",
        }
        _AEROPORT_KEYS = {"aerodrome", "piste_d_aerodrome"}
        _OUTDOOR_EXTRA_KEYS = {"terrain_de_sport", "cimetiere"}
        _HYDRO_KEYS = {"water_surface", "rivers", "reservoir"}
        _ZONES_KEYS = {"natura_sic", "natura_zps", "znieff1", "znieff2", "apb", "pnr"}
        rpg_grp = None  # créé à la demande au premier passage d'une couche RPG
        parcels_layer_deferred = (
            None  # ajouté à rpg_grp EN DERNIER (sous toutes les couches agri)
        )
        aeroport_grp = None  # créé à la demande
        outdoor_grp = (
            None  # créé à la demande (ZAI outdoor_layers ou terrain/cimetière)
        )
        hydro_grp = None  # créé à la demande au premier passage d'une couche hydro
        transport_grp = None  # créé à la demande pour les équipements de transport
        constr_grp = None  # créé à la demande pour les constructions surfaciques
        # Zonages de protection : en tête de la liste d'ordre → le sous-groupe
        # est créé en premier et apparaît donc en haut de la légende.
        zones_grp = None  # créé à la demande pour les zonages de protection

        for entry in selected_entries:
            sk = entry["style_key"]
            if sk not in loaded_layers:
                continue
            layer = loaded_layers[sk]

            # ── Couches agricoles → sous-groupe dédié ────────────────────────
            if sk in _RPG_KEYS:
                if rpg_grp is None:
                    rpg_grp = group.addGroup("Agriculture")
                if entry.get("style") is not None:
                    self._apply_custom_style(layer, entry["style"], entry["geom_type"])
                else:
                    self._apply_style(layer, sk)
                QgsProject.instance().addMapLayer(layer, False)
                rpg_grp.addLayer(layer)
                continue

            # ── Aéroport → sous-groupe dédié ─────────────────────────────────
            if sk in _AEROPORT_KEYS:
                if aeroport_grp is None:
                    aeroport_grp = group.addGroup("Aéroport")
                if entry.get("style") is not None:
                    self._apply_custom_style(layer, entry["style"], entry["geom_type"])
                else:
                    self._apply_style(layer, sk)
                QgsProject.instance().addMapLayer(layer, False)
                aeroport_grp.addLayer(layer)
                continue

            # ── Espaces publics (terrain de sport, cimetière) → groupe dédié ──
            if sk in _OUTDOOR_EXTRA_KEYS:
                if outdoor_grp is None:
                    outdoor_grp = group.addGroup("Espaces publics extérieurs")
                if entry.get("style") is not None:
                    self._apply_custom_style(layer, entry["style"], entry["geom_type"])
                else:
                    self._apply_style(layer, sk)
                QgsProject.instance().addMapLayer(layer, False)
                outdoor_grp.addLayer(layer)
                continue

            # ── Sous-groupes programmatiques ─────────────────────────────────
            if sk == "buildings":
                # Données statistiques EN PREMIER → juste sous Végétation haute dans la légende
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
                if transport_grp is None:
                    transport_grp = group.addGroup("Équipements de transport")
                for t_layer in transport_layers:
                    QgsProject.instance().addMapLayer(t_layer, False)
                    transport_grp.addLayer(t_layer)
                continue  # sublayers remplacent la couche plate

            if sk == "construction_surfacique":
                constr_layers = build_construction_surfacique_layers(layer, feedback)
                feedback.pushInfo(f"   ✓  {len(constr_layers)} type(s) de construction")
                for c_layer in constr_layers:
                    QgsProject.instance().addMapLayer(c_layer, False)
                    if c_layer.name() in _PONT_NATURES:
                        if transport_grp is None:
                            transport_grp = group.addGroup("Équipements de transport")
                        transport_grp.addLayer(c_layer)
                    else:
                        if constr_grp is None:
                            constr_grp = group.addGroup("Constructions surfaciques")
                        constr_grp.addLayer(c_layer)
                continue  # sublayers remplacent la couche plate

            # ── Symbologie ───────────────────────────────────────────────────
            if sk in ("sirene", "zai"):
                self._apply_style(layer, sk)
            elif sk == "osm":
                pass  # QgsRasterLayer — pas de symbologie vectorielle
            elif entry.get("style") is not None:
                self._apply_custom_style(layer, entry["style"], entry["geom_type"])
            else:
                self._apply_style(layer, sk)

            # Espaces publics extérieurs doit apparaître AU-DESSUS de ZAI dans la légende.
            # On crée le groupe AVANT d'ajouter ZAI au groupe parent — les nœuds ajoutés
            # en premier apparaissent en haut dans l'arbre QGIS.
            if sk == "zai" and outdoor_layers and outdoor_grp is None:
                outdoor_grp = group.addGroup("Espaces publics extérieurs")

            QgsProject.instance().addMapLayer(layer, False)

            # ── Routage vers sous-groupe ──────────────────────────────────────
            if sk in _ZONES_KEYS:
                if zones_grp is None:
                    zones_grp = group.addGroup("Zonages de protection")
                zones_grp.addLayer(layer)
            elif sk in _HYDRO_KEYS:
                # Hydrographie (surface, cours d'eau, réservoir) → groupe dédié.
                if hydro_grp is None:
                    hydro_grp = group.addGroup("Hydrographie")
                hydro_grp.addLayer(layer)
            elif sk == "parcels":
                # Parcelles cadastrales → différé : ajoutées à rpg_grp après la boucle
                # pour qu'elles apparaissent en BAS du groupe Agriculture.
                parcels_layer_deferred = layer
            else:
                group.addLayer(layer)

            # Peuple outdoor_grp avec les couches ZAI outdoor (groupe déjà créé ci-dessus).
            if sk == "zai" and outdoor_layers:
                for o_layer in outdoor_layers:
                    QgsProject.instance().addMapLayer(o_layer, False)
                    outdoor_grp.addLayer(o_layer)

        # Parcelles cadastrales ajoutées APRÈS la boucle dans le groupe principal —
        # ainsi elles apparaissent sous le groupe Agriculture sans en faire partie.
        if parcels_layer_deferred is not None:
            group.addLayer(parcels_layer_deferred)

        feedback.pushInfo(f"✅  {len(loaded_layers)} couche(s) dans « {nom} »")
        feedback.setProgress(90)

        # ── 6. Proposition d'enregistrement .qgz ─────────────────────────────
        if show_save_dialog:
            reply = QMessageBox.question(
                None,
                "Fond de plan prêt",
                f"✅  Le fond de plan de {nom} est prêt !\n\n"
                "Enregistrer le projet en fichier .qgz ?",
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
                    feedback.pushInfo(f"💾  Projet enregistré")

        # ── 7. Gestionnaire de thèmes ─────────────────────────────────────────
        # Ouvre (ou retrouve) le panneau de thèmes dans la session QGIS courante.
        # Idempotent : plusieurs exécutions du script ne créent qu'un seul dock.
        try:
            from qgis.utils import iface as _iface

            if _iface is not None:
                ensure_theme_manager(_iface)
                feedback.pushInfo("🎛  Panneau « Contrôle de visibilité » ouvert.")
        except Exception:
            # Hors contexte GUI (tests headless) on continue sans le dock,
            # mais on signale l'erreur au lieu de la masquer.
            feedback.reportError(
                "⚠  Impossible d'ouvrir le gestionnaire de thèmes :\n"
                + traceback.format_exc(),
                fatalError=False,
            )

        feedback.setProgress(100)
        feedback.pushInfo("🎉  Fond de plan prêt !")

    # =========================================================================
    # Helper – recherche et sélection de commune
    # =========================================================================

    # ── API Géo (geo.api.gouv.fr) — source unique + fetch partagé ─────────────
    _GEO_API_BASE   = "https://geo.api.gouv.fr/communes"
    _GEO_API_FIELDS = "fields=nom,code,contour&format=geojson&geometry=contour"
    _GEO_API_TYPES  = "type=commune-actuelle,arrondissement-municipal"

    @staticmethod
    def _geo_api_fetch(url):
        """GET une URL de l'API Géo, 4 tentatives avec backoff exponentiel.
        Retourne la liste des 'features' GeoJSON (lève après le 4e échec)."""
        for attempt in range(4):
            try:
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                return resp.json().get("features", [])
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)

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
        _BASE, _FIELDS, _TYPES = (
            self._GEO_API_BASE, self._GEO_API_FIELDS, self._GEO_API_TYPES
        )
        _fetch = self._geo_api_fetch

        features = []
        seen_codes = set()

        def _merge(new_feats):
            for feat in new_feats:
                code = feat["properties"].get("code", "")
                if code and code not in seen_codes:
                    seen_codes.add(code)
                    features.append(feat)

        try:
            # Passe 1 — recherche par nom (toujours)
            _merge(
                _fetch(
                    f"{_BASE}?nom={requests.utils.quote(nom_input)}&{_FIELDS}&{_TYPES}"
                )
            )

            # Passe 2 — si l'entrée ressemble à un code postal (5 chiffres)
            if nom_input.isdigit() and len(nom_input) == 5:
                _merge(_fetch(f"{_BASE}?codePostal={nom_input}&{_FIELDS}&{_TYPES}"))

            # Passe 3 — si l'entrée ressemble à un code INSEE (4–5 chars alphanum)
            # Couvre les codes normaux (ex. "75119"), la Corse ("2A004"), les DOM ("97209")
            if 4 <= len(nom_input) <= 5 and nom_input.replace("-", "").isalnum():
                _merge(
                    _fetch(
                        f"{_BASE}?code={requests.utils.quote(nom_input.upper())}&{_FIELDS}&{_TYPES}"
                    )
                )

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
    # Helper – résolution silencieuse (sans dialogue) d'un nom en commune
    # =========================================================================

    def _lookup_commune_batch(self, nom, insee_hint, feedback, warn_on_miss=False):
        """
        Résout un nom/code commune → dict {nom, code, geometry} sans dialogue.
        Retourne None si introuvable ou ambigu.
        Priorité : code INSEE direct → lookup par nom exact.
        """
        _BASE, _FIELDS, _TYPES = (
            self._GEO_API_BASE, self._GEO_API_FIELDS, self._GEO_API_TYPES
        )
        _get = self._geo_api_fetch

        def _to_dict(feat):
            p = feat["properties"]
            return {"nom": p["nom"], "code": p["code"], "geometry": feat["geometry"]}

        def _looks_like_insee(s):
            # Code métropole : 5 chiffres
            if s.isdigit() and len(s) == 5:
                return True
            # Corse : 2A/2B + 3 chiffres
            if len(s) == 5 and s[:2].upper() in ("2A", "2B") and s[2:].isdigit():
                return True
            # DOM-TOM : commence par 97 + 3 chiffres (ex. 97209)
            if len(s) == 5 and s[:2] == "97" and s[2:].isdigit():
                return True
            return False

        try:
            # 1. Lookup direct par code INSEE (fiable, commune déjà connue)
            if insee_hint:
                feats = _get(
                    f"{_BASE}?code={requests.utils.quote(insee_hint)}&{_FIELDS}&{_TYPES}"
                )
                if feats:
                    return _to_dict(feats[0])

            # 2. L'entrée ressemble à un code INSEE → lookup direct avant le nom
            if _looks_like_insee(nom):
                feats = _get(
                    f"{_BASE}?code={requests.utils.quote(nom.upper())}&{_FIELDS}&{_TYPES}"
                )
                if len(feats) == 1:
                    return _to_dict(feats[0])

            # 3. Lookup par nom
            feats = _get(f"{_BASE}?nom={requests.utils.quote(nom)}&{_FIELDS}&{_TYPES}")
            if not feats:
                if warn_on_miss:
                    feedback.reportError(
                        f"   ⚠  « {nom} » introuvable dans l'API Géo.", fatalError=False
                    )
                return None

            # Correspondance exacte (insensible à la casse)
            for feat in feats:
                if feat["properties"]["nom"].lower() == nom.lower():
                    return _to_dict(feat)

            # Résultat unique non exact → on le prend
            if len(feats) == 1:
                return _to_dict(feats[0])

            if warn_on_miss:
                feedback.reportError(
                    f"   ⚠  Plusieurs communes correspondent à « {nom} » — ignorée. "
                    "Utilisez le code INSEE pour lever l'ambiguïté.",
                    fatalError=False,
                )
            return None

        except Exception as e:
            if warn_on_miss:
                feedback.reportError(f"   ⚠  API Géo ({nom}) : {e}", fatalError=False)
            return None

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

    # ── Robustesse du téléchargement WFS ──────────────────────────────────────
    # Depuis septembre 2026, la passerelle data.geopf.fr (Kong) renvoie
    # sporadiquement (≈ 1 % des requêtes, au hasard) une page de métriques
    # Prometheus/JMX (HTTP 200, text/plain) à la place d'une page GML de 5 000
    # entités — avec « cache-control: private, max-age=1814400 » (21 jours).
    # Le cache réseau disque de QGIS la conserve donc, et la ressert à toute
    # requête de la même URL : les trois relances internes du fournisseur WFS
    # (qui journalise « Erreur lors de l'analyse de la réponse GetFeature »),
    # une couche recréée, et même le prochain import de la même commune. Le
    # fournisseur abandonne alors avec « Le téléchargement des entités … a
    # échoué ou partiellement échoué » — et l'itérateur s'arrête SANS
    # exception sur les pages déjà reçues. Résultat : couche tronquée en
    # silence, d'un seul bloc (les pages suivent l'ordre des identifiants, donc
    # sont groupées géographiquement). Le bâti du Mans fait 19 pages : ~17 %
    # des imports perdaient « la moitié de la ville », puis la perdaient à
    # chaque nouvel essai.
    #
    # Parade : télécharger ici l'emprise complète (la requête exacte que
    # native:clip émettra ensuite, servie alors depuis le cache du
    # fournisseur), lire provider.errors() — indépendant de la langue de QGIS —
    # et, en cas d'échec, vider le cache réseau puis recréer la couche après
    # un délai. Après épuisement des tentatives, erreur explicite plutôt que
    # découpage de données partielles.
    _WFS_RETRY_DELAYS = (2, 5, 10)  # secondes d'attente avant chaque nouvel essai

    @staticmethod
    def _wfs_download(layer, rect):
        """
        Force le téléchargement complet de `rect` (toute la couche si None)
        et renvoie (nb_entités, message_d_erreur_ou_None).
        """
        provider = layer.dataProvider()
        provider.clearErrors()
        request = QgsFeatureRequest()
        if rect is not None:
            request.setFilterRect(rect)
        n = sum(1 for _ in layer.getFeatures(request))
        # L'erreur du téléchargeur (thread séparé) parvient au fournisseur par
        # signal différé : dépiler la boucle d'événements avant de la lire.
        for _ in range(3):
            QCoreApplication.processEvents()
            time.sleep(0.05)
        errors = provider.errors()
        return n, (errors[-1] if errors else None)

    @staticmethod
    def _purge_network_cache():
        """
        Vide le cache réseau disque de QGIS, qui conserve 21 jours la page
        corrompue. Un retrait ciblé (cache.remove(QUrl)) n'est pas fiable :
        la clé ne correspond pas toujours à l'URL journalisée (encodage des
        apostrophes du filtre CQL des parcelles, notamment). Le cache ne
        contient que des tuiles (OSM…) et des pages WFS : le vider ne coûte
        que des re-téléchargements, pour un échec rare (~1 % des requêtes).
        """
        cache = QgsNetworkAccessManager.instance().cache()
        if cache is not None:
            cache.clear()

    @staticmethod
    def _sleep_cancellable(seconds, feedback):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not feedback.isCanceled():
            time.sleep(0.25)

    def _wfs_open(self, uri, display_name, rect, feedback):
        """
        Crée la couche WFS `uri` et télécharge `rect` de façon vérifiée, en
        recréant la couche après un délai si Géoplateforme a renvoyé une
        réponse corrompue.

        Renvoie (layer, nb_entités) — ou None si la couche reste invalide
        (typename inconnu…), comme avant. Lève une exception si le
        téléchargement reste incomplet : jamais de couche partielle.
        """
        n_attempts = len(self._WFS_RETRY_DELAYS) + 1
        problem = None
        for attempt in range(1, n_attempts + 1):
            if attempt > 1:
                delay = self._WFS_RETRY_DELAYS[attempt - 2]
                feedback.pushWarning(
                    f"   ⚠  {display_name} — {problem} "
                    f"(tentative {attempt - 1}/{n_attempts}), "
                    f"nouvel essai dans {delay} s…"
                )
                self._sleep_cancellable(delay, feedback)
                if feedback.isCanceled():
                    break
            layer = QgsVectorLayer(uri, display_name, "WFS")
            if not layer.isValid():
                problem = "couche invalide"
                continue
            n, error = self._wfs_download(layer, rect)
            if error is None:
                return layer, n
            problem = f"téléchargement incomplet ({error})"
            self._purge_network_cache()

        if feedback.isCanceled() or problem == "couche invalide":
            return None
        raise Exception(
            f"{display_name} : Géoplateforme WFS en échec après {n_attempts} "
            f"tentatives — {problem}. Relancer l'import."
        )

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
        Construit l'URI WFS GetFeature avec filtre BBOX, charge la couche
        (téléchargement complet vérifié, voir _wfs_open), puis la découpe sur
        le contour communal.
        Renvoie None (avec avertissement) si la couche est invalide ; lève une
        exception si Géoplateforme ne livre pas la couche complète.
        """
        # URI WFS — le paramètre BBOX attend : minX,minY,maxX,maxY,CRS.
        # NB : le fournisseur WFS de QGIS ne conserve pas la valeur de BBOX
        # d'une URI de cette forme (il note seulement qu'une restriction
        # spatiale est demandée) ; l'emprise effectivement téléchargée est
        # celle du filterRect de la requête — voir _wfs_open et native:clip.
        bbox_str = (
            f"{bbox.xMinimum()},{bbox.yMinimum()},"
            f"{bbox.xMaximum()},{bbox.yMaximum()},EPSG:2154"
        )
        uri = (
            f"{_WFS_URL}"
            "?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
            f"&TYPENAME={typename}&SRSNAME=EPSG:2154&BBOX={bbox_str}"
        )

        opened = self._wfs_open(uri, display_name, bbox, feedback)
        if opened is None:
            if not feedback.isCanceled():
                feedback.pushWarning(f"⚠  {display_name} — couche invalide")
            return None
        layer, n_downloaded = opened
        feedback.pushInfo(f"   ✓  {n_downloaded} entité(s) téléchargée(s)")

        # Découpage sur le contour communal. native:clip redemande l'emprise
        # que _wfs_open vient de télécharger : servie depuis le cache du
        # fournisseur, sans nouvel aller-retour réseau.
        # Certaines couches WFS (notamment RPG) contiennent des géométries
        # invalides (auto-intersections, doublons de sommets…). On passe un
        # QgsProcessingContext avec GeometrySkipInvalid pour que native:clip
        # ignore ces entités au lieu d'interrompre le traitement.
        layer.dataProvider().createSpatialIndex()
        clip_ctx = QgsProcessingContext()
        clip_ctx.setInvalidGeometryCheck(QgsFeatureRequest.GeometrySkipInvalid)
        clipped = processing.run(
            "native:clip",
            {"INPUT": layer, "OVERLAY": boundary_layer, "OUTPUT": "memory:"},
            context=clip_ctx,
            feedback=feedback,
        )["OUTPUT"]
        clipped.setName(display_name)

        # La végétation BDTOPO fait doublon avec la BD Forêt V2 sur les
        # formations forestières : ces natures sont retirées de la couche
        # (la BD Forêt, plus fine et catégorisée, les couvre). Fait ici, dans
        # le chargeur partagé, pour que fdp_ajout_couches en bénéficie aussi.
        if typename == "BDTOPO_V3:zone_de_vegetation":
            ids = [
                f.id()
                for f in clipped.getFeatures()
                if f["nature"] in _VEGETATION_NATURES_EXCLUES
            ]
            if ids:
                clipped.dataProvider().deleteFeatures(ids)
                feedback.pushInfo(
                    f"   ✂  Végétation basse : {len(ids)} entité(s) forestière(s) "
                    "retirée(s) (couvertes par la BD Forêt V2)"
                )

        return clipped

    # Garde-fou : au-delà de ce nombre d'entités, le filtre code_insee n'a
    # manifestement pas été appliqué (récupération nationale) → repli bbox+clip.
    _PARCELLE_MAX = 500000

    def _load_parcelle_layer(self, typename, display_name, insee, bbox,
                             boundary_layer, crs_2154, feedback):
        """
        Charge les parcelles cadastrales via un FILTRE ATTRIBUTAIRE
        (CQL_FILTER code_insee='INSEE') au lieu d'un filtre BBOX + découpage.

        Les parcelles cadastrales sont rattachées administrativement à une seule
        commune (elles ne franchissent jamais une limite communale), donc le
        filtre code_insee renvoie exactement le même ensemble que bbox+clip, tout
        en évitant (a) la sur-collecte des parcelles voisines et surtout (b) le
        native:clip de dizaines de milliers de polygones — l'étape la plus lente.

        Résultat matérialisé en couche mémoire (pas de couche WFS vive dans le
        projet). Repli automatique sur bbox+clip si le filtre renvoie 0 (Paris/
        Lyon/Marseille entières : parcelles taguées par arrondissement) ou un
        nombre invraisemblable (filtre non honoré par le serveur).
        """
        from urllib.parse import quote

        cql = quote(f"code_insee='{insee}'")
        uri = (
            f"{_WFS_URL}"
            "?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
            f"&TYPENAME={typename}&SRSNAME=EPSG:2154&CQL_FILTER={cql}"
        )

        def _fallback(reason):
            feedback.pushInfo(f"   ↩  Parcelles : {reason} — repli bbox + découpage.")
            return self._load_wfs_layer(
                typename, display_name, bbox, boundary_layer, crs_2154, feedback
            )

        # Même parade que _wfs_open contre les réponses corrompues de
        # Géoplateforme : téléchargement vérifié, vidage du cache, nouvelle
        # couche après délai. Un seul nouvel essai ici : au-delà, le repli
        # bbox + découpage (parade complète) est préférable à l'attente.
        n_attempts = 2
        problem = None
        try:
            for attempt in range(1, n_attempts + 1):
                if attempt > 1:
                    delay = self._WFS_RETRY_DELAYS[attempt - 2]
                    feedback.pushWarning(
                        f"   ⚠  {display_name} — {problem} "
                        f"(tentative {attempt - 1}/{n_attempts}), "
                        f"nouvel essai dans {delay} s…"
                    )
                    self._sleep_cancellable(delay, feedback)
                    if feedback.isCanceled():
                        break

                layer = QgsVectorLayer(uri, display_name, "WFS")
                n = layer.featureCount() if layer.isValid() else 0
                if n <= 0 or n > self._PARCELLE_MAX:
                    return _fallback(f"filtre code_insee inutilisable (n={n})")

                # Sécurité : vérifier que le filtre s'applique bien aux ENTITÉS (pas
                # seulement au comptage). On lit une entité témoin ; si son code_insee
                # ne correspond pas, le provider ignore le CQL → repli (évite un
                # téléchargement national qui ferait exploser la mémoire).
                sample = next(layer.getFeatures(QgsFeatureRequest().setLimit(1)), None)
                if sample is None or str(sample["code_insee"]) != str(insee):
                    got = None if sample is None else sample["code_insee"]
                    return _fallback(f"filtre non appliqué aux entités (témoin={got})")

                n_downloaded, error = self._wfs_download(layer, None)
                if error is not None:
                    problem = f"téléchargement incomplet ({error})"
                    self._purge_network_cache()
                    continue

                # Matérialisation en mémoire (copie directe, sans découpage
                # géométrique) — lue depuis le cache du fournisseur.
                mem = QgsVectorLayer(
                    f"{QgsWkbTypes.displayString(layer.wkbType())}?crs=EPSG:2154",
                    display_name,
                    "memory",
                )
                pr = mem.dataProvider()
                pr.addAttributes(layer.fields().toList())
                mem.updateFields()
                pr.addFeatures(list(layer.getFeatures()))
                mem.updateExtents()
                feedback.pushInfo(
                    f"   ✓  {mem.featureCount()} parcelles (filtre code_insee)"
                )
                return mem
        except Exception as e:
            return _fallback(f"voie rapide en erreur ({e})")
        if feedback.isCanceled():
            return None
        return _fallback(f"Géoplateforme en échec après {n_attempts} tentatives ({problem})")

    # =========================================================================
    # Helper – SIRENE
    # =========================================================================

    @staticmethod
    def _resolve_datagouv_parquet_url(dataset_slug, title_needle, exclude_needle,
                                      fallback_url, feedback):
        """Résout l'URL de la ressource parquet courante d'un jeu de données
        data.gouv (les URLs sont horodatées et changent chaque mois). Renvoie
        fallback_url si l'API est injoignable ou si aucune ressource ne convient."""
        api = f"https://www.data.gouv.fr/api/1/datasets/{dataset_slug}/"
        try:
            r = requests.get(api, timeout=30)
            r.raise_for_status()
            for res in r.json().get("resources", []):
                if (res.get("format") or "").lower() != "parquet":
                    continue
                title = res.get("title") or ""
                if title_needle and title_needle.lower() not in title.lower():
                    continue
                if exclude_needle and exclude_needle.lower() in title.lower():
                    continue
                if res.get("url"):
                    return res["url"]
        except Exception as e:
            feedback.pushWarning(
                f"⚠  Résolution URL data.gouv impossible ({e}) — URL de secours utilisée."
            )
        return fallback_url

    @staticmethod
    def _ensure_parquet_cache(cache_file, url, label, max_age, feedback):
        """Assure la présence d'un parquet en cache local. Télécharge s'il est
        absent ou périmé (> max_age). Si le téléchargement échoue mais qu'un
        cache existe déjà, on le conserve (même périmé) plutôt que tout perdre.
        Retourne True si un fichier utilisable est présent à la fin."""
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        exists = os.path.exists(cache_file)
        stale = exists and (time.time() - os.path.getmtime(cache_file)) > max_age
        if exists and not stale:
            return True
        if stale:
            feedback.pushInfo(f"   ⬇  Rafraîchissement du cache {label} (> 35 j)…")
        else:
            feedback.pushInfo(
                f"   ⬇  Téléchargement {label} (~1–2 Go, opération unique)…"
            )
        tmp = cache_file + ".tmp"
        try:
            with requests.get(url, stream=True, timeout=300) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                done = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                        if feedback.isCanceled():
                            os.remove(tmp)
                            return False
                        f.write(chunk)
                        done += len(chunk)
                        if total and done % (200 * 1024 * 1024) < 4 * 1024 * 1024:
                            feedback.pushInfo(
                                f"      {done // (1024 * 1024)} / {total // (1024 * 1024)} Mo"
                            )
            os.replace(tmp, cache_file)
            feedback.pushInfo(f"   ✓  Cache {label} à jour")
            return True
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            if exists:
                feedback.pushWarning(
                    f"⚠  Rafraîchissement {label} impossible ({e}). "
                    "Utilisation du cache local existant (peut être périmé)."
                )
                return True
            feedback.reportError(
                f"SIRENE — téléchargement {label} impossible et aucun cache local : {e}",
                fatalError=False,
            )
            return False

    # Colonnes lues dans StockEtablissement — partagées par _load_sirene et
    # _prefetch_sirene (doivent rester identiques pour que le filtrage en mémoire
    # du lot pré-chargé expose exactement les mêmes colonnes).
    _SIRENE_STOCK_COLS = [
        "siret",
        "codeCommuneEtablissement",
        "etatAdministratifEtablissement",
        "activitePrincipaleEtablissement",
        "enseigne1Etablissement",
        "denominationUsuelleEtablissement",
        "numeroVoieEtablissement",
        "indiceRepetitionEtablissement",
        "typeVoieEtablissement",
        "libelleVoieEtablissement",
        "codePostalEtablissement",
        "libelleCommuneEtablissement",
    ]

    def _sirene_read(self, cache_file, columns, commune_col, insee_codes,
                     extra_filters, memo_key, label, feedback):
        """
        Lit une table SIRENE (colonnes `columns`) filtrée par commune.

        Si un lot a été pré-chargé (self._sirene_batch) et qu'il couvre TOUTES
        les communes demandées, filtre ce lot en mémoire au lieu de rebalayer le
        parquet (import en lot — voir docs/PERF.md). Sinon lit depuis le disque,
        comportement identique à l'import mono-commune.

        Renvoie la table pyarrow, ou None (erreur signalée).
        """
        import pyarrow.parquet as pq

        batch = getattr(self, "_sirene_batch", None)
        if (batch is not None and memo_key in batch
                and set(insee_codes) <= batch["codes"]):
            import pyarrow as pa
            import pyarrow.compute as pc

            tbl = batch[memo_key]
            mask = pc.is_in(tbl.column(commune_col), value_set=pa.array(insee_codes))
            return tbl.filter(mask)

        if len(insee_codes) == 1:
            dnf = [(commune_col, "=", insee_codes[0])]
        else:
            dnf = [(commune_col, "in", insee_codes)]
        try:
            return pq.read_table(cache_file, columns=columns, filters=dnf + extra_filters)
        except Exception as e:
            feedback.reportError(
                f"SIRENE — lecture {label} : {e}\n{traceback.format_exc()}",
                fatalError=False,
            )
            return None

    def _prefetch_sirene(self, all_codes, feedback):
        """
        Pré-charge en UN seul balayage les données SIRENE de tout un lot de
        communes (au lieu d'un balayage du parquet par commune — voir docs/PERF.md).
        Peuple self._sirene_batch ; _load_sirene/_sirene_read filtrent ensuite
        chaque commune en mémoire.

        Les communes PLM (Paris/Lyon/Marseille entières → codes arrondissement)
        sont exclues et lues à la demande. Silencieusement ignoré si aucun cache
        local n'est présent (le téléchargement sera géré par commune).
        """
        import pyarrow.parquet as pq

        _PLM = {"75056", "69123", "13055"}
        codes = sorted({c for c in all_codes if c and c not in _PLM})
        if len(codes) < 2:
            return  # rien à gagner sous 2 communes

        # Chemins de cache — DOIVENT correspondre à ceux de _load_sirene.
        cache_dir   = os.path.join(os.path.expanduser("~"), ".sirene")
        stock_cache = os.path.join(cache_dir, "StockEtablissement.parquet")
        geo_cache   = os.path.join(cache_dir, "GeolocEtablissement.parquet")
        if not os.path.exists(stock_cache):
            return

        try:
            schema = set(pq.ParquetFile(stock_cache).schema_arrow.names)
        except Exception:
            return
        legacy = "coordonneeLambertAbscisseEtablissement" in schema

        stock_cols = list(self._SIRENE_STOCK_COLS)
        if legacy:
            stock_cols += [
                "coordonneeLambertAbscisseEtablissement",
                "coordonneeLambertOrdonneeEtablissement",
            ]

        feedback.pushInfo(
            f"   ⚡  Pré-chargement SIRENE — {len(codes)} communes en un balayage…"
        )
        batch = {"codes": set(codes)}
        try:
            batch["stock"] = pq.read_table(
                stock_cache,
                columns=stock_cols,
                filters=[
                    ("codeCommuneEtablissement", "in", codes),
                    ("etatAdministratifEtablissement", "=", "A"),
                ],
            )
            if not legacy and os.path.exists(geo_cache):
                # PLG_CODE_COMMUNE inclus : sert au filtrage en mémoire par commune.
                batch["geoloc"] = pq.read_table(
                    geo_cache,
                    columns=["SIRET", "X", "Y", "EPSG", "PLG_CODE_COMMUNE"],
                    filters=[("PLG_CODE_COMMUNE", "in", codes)],
                )
        except Exception as e:
            feedback.pushWarning(
                f"⚠  Pré-chargement SIRENE ignoré ({e}) — lecture par commune."
            )
            return
        self._sirene_batch = batch

    def _load_sirene(
        self,
        insee: str,
        boundary_layer: QgsVectorLayer,
        crs_2154: QgsCoordinateReferenceSystem,
        feedback,
    ):
        """
        Charge les établissements SIRENE en croisant deux fichiers nationaux INSEE
        mis en cache dans ~/.sirene/ :
          • StockEtablissement — NAF, enseigne/dénomination, commune, état ;
          • Géolocalisation    — SIRET → coordonnées X/Y (Lambert 93 en métropole).
        Les deux sont filtrés par commune puis joints sur le SIRET. Les URLs
        (horodatées, mensuelles) sont résolues via l'API data.gouv avec une URL de
        secours, et un cache périmé est conservé si le téléchargement échoue.

        Rétro-compatibilité : si le cache StockEtablissement contient déjà les
        colonnes de coordonnées (ancien fichier fusionné « géolocalisé BAN »,
        produit arrêté en avril 2026), on lit les coordonnées directement sans
        télécharger le fichier de géolocalisation.
        """
        import pyarrow.parquet as pq

        _CACHE_DIR   = os.path.join(os.path.expanduser("~"), ".sirene")
        _STOCK_CACHE = os.path.join(_CACHE_DIR, "StockEtablissement.parquet")
        _GEO_CACHE   = os.path.join(_CACHE_DIR, "GeolocEtablissement.parquet")
        _MAX_AGE     = 35 * 24 * 3600  # 35 j — INSEE publie mensuellement

        _STOCK_DATASET = (
            "base-sirene-des-entreprises-et-de-leurs-etablissements-siren-siret"
        )
        _STOCK_FALLBACK = (
            "https://static.data.gouv.fr/resources/"
            "base-sirene-des-entreprises-et-de-leurs-etablissements-siren-siret/"
            "20260701-093629/stock-stocketablissement-parquet.parquet"
        )
        _GEO_DATASET = (
            "geolocalisation-des-etablissements-du-repertoire-sirene-pour-les-etudes-statistiques"
        )
        _GEO_FALLBACK = (
            "https://static.data.gouv.fr/resources/"
            "geolocalisation-des-etablissements-du-repertoire-sirene-pour-les-etudes-statistiques/"
            "20260621-112946/geoloc-geolocalisationetablissement-sirene-pour-etudes-statistiques-parquet.parquet"
        )

        # ── StockEtablissement : réutiliser un ancien fichier fusionné s'il ──
        #    existe déjà (il contient les coordonnées), sinon (re)télécharger le
        #    fichier stock simple. Le produit fusionné « géolocalisé BAN » étant
        #    arrêté, on ne remplace JAMAIS un cache fusionné valide par le stock
        #    simple (qui, lui, exige en plus le fichier de géolocalisation).
        #    Pour forcer le passage aux fichiers à jour : supprimer ce cache.
        legacy_geo = False
        if os.path.exists(_STOCK_CACHE):
            try:
                _schema = set(pq.ParquetFile(_STOCK_CACHE).schema_arrow.names)
                legacy_geo = "coordonneeLambertAbscisseEtablissement" in _schema
            except Exception:
                legacy_geo = False

        if legacy_geo:
            feedback.pushInfo(
                "   ℹ  Cache SIRENE fusionné détecté — réutilisé (aucun téléchargement)."
            )
        else:
            stock_url = self._resolve_datagouv_parquet_url(
                _STOCK_DATASET, "StockEtablissement", "Historique",
                _STOCK_FALLBACK, feedback,
            )
            if not self._ensure_parquet_cache(
                _STOCK_CACHE, stock_url, "StockEtablissement", _MAX_AGE, feedback
            ):
                return None
            if feedback.isCanceled():
                return None

        # ── Paris / Lyon / Marseille : les établissements sont stockés sous les
        #    codes arrondissement, pas sous le code commune parent.
        _PLM = {
            "75056": (
                [f"751{str(i).zfill(2)}" for i in range(1, 21)],
                "Paris",
                "~1 000 000",
                "20–40 min",
            ),
            "69123": (
                ["6938" + str(i) for i in range(1, 10)],
                "Lyon",
                "~550 000",
                "5–15 min",
            ),
            "13055": (
                [f"132{str(i).zfill(2)}" for i in range(1, 17)],
                "Marseille",
                "~300 000",
                "10–20 min",
            ),
        }
        if insee in _PLM:
            arr_codes, city_name, est_count, est_time = _PLM[insee]
            reply = QMessageBox.warning(
                None,
                f"Import {city_name} — ville entière",
                f"Vous importez {city_name} en tant que commune entière.\n\n"
                f"Cela représente environ {est_count} établissements répartis sur "
                f"{len(arr_codes)} arrondissements.\n\n"
                f"Le traitement (appariement bâtiments + déplacement) prendra "
                f"environ {est_time}.\n\n"
                f"Pour un import rapide, relancez en cherchant directement "
                f"l'arrondissement (ex. « {city_name} 3e »).\n\n"
                f"Continuer quand même ?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                feedback.pushInfo("   Import SIRENE annulé par l'utilisateur.")
                return None
            insee_codes = arr_codes
        else:
            insee_codes = [insee]

        # ── Lecture StockEtablissement filtré par commune ────────────────────
        label = (
            insee if len(insee_codes) == 1 else f"{insee_codes[0]}–{insee_codes[-1]}"
        )
        feedback.pushInfo(f"   🔍  Filtrage SIRENE commune {label}…")

        stock_cols = list(self._SIRENE_STOCK_COLS)
        if legacy_geo:
            stock_cols += [
                "coordonneeLambertAbscisseEtablissement",
                "coordonneeLambertOrdonneeEtablissement",
            ]
        table = self._sirene_read(
            _STOCK_CACHE, stock_cols, "codeCommuneEtablissement", insee_codes,
            [("etatAdministratifEtablissement", "=", "A")], "stock",
            "StockEtablissement", feedback,
        )
        if table is None:
            return None

        # ── Colonnes attributaires en listes Python ──────────────────────────
        sirets = table["siret"].to_pylist()
        nafs = table["activitePrincipaleEtablissement"].to_pylist()
        enseignes = table["enseigne1Etablissement"].to_pylist()
        denoms = table["denominationUsuelleEtablissement"].to_pylist()
        nums = table["numeroVoieEtablissement"].to_pylist()
        reps = table["indiceRepetitionEtablissement"].to_pylist()
        types_voie = table["typeVoieEtablissement"].to_pylist()
        libelles = table["libelleVoieEtablissement"].to_pylist()
        cps = table["codePostalEtablissement"].to_pylist()
        communes = table["libelleCommuneEtablissement"].to_pylist()

        # ── Coordonnées : soit du fichier fusionné, soit du fichier géoloc ────
        # coords : { siret -> (x, y) }.  epsg : CRS des coords (2154 en métropole).
        epsg = "2154"
        if legacy_geo:
            _xs = table["coordonneeLambertAbscisseEtablissement"].to_pylist()
            _ys = table["coordonneeLambertOrdonneeEtablissement"].to_pylist()
            coords = {s: (x, y) for s, x, y in zip(sirets, _xs, _ys)}
        else:
            geo_url = self._resolve_datagouv_parquet_url(
                _GEO_DATASET, None, None, _GEO_FALLBACK, feedback
            )
            if not self._ensure_parquet_cache(
                _GEO_CACHE, geo_url, "Géolocalisation SIRENE", _MAX_AGE, feedback
            ):
                return None
            if feedback.isCanceled():
                return None
            geo = self._sirene_read(
                _GEO_CACHE, ["SIRET", "X", "Y", "EPSG"], "PLG_CODE_COMMUNE",
                insee_codes, [], "geoloc", "Géolocalisation", feedback,
            )
            if geo is None:
                return None
            coords = {
                s: (x, y)
                for s, x, y in zip(
                    geo["SIRET"].to_pylist(),
                    geo["X"].to_pylist(),
                    geo["Y"].to_pylist(),
                )
            }
            for _e in geo["EPSG"].to_pylist():
                if _e:
                    epsg = str(_e).strip()
                    break

        # ── Couche mémoire dans le CRS des coordonnées (2154 en métropole) ───
        mem_layer = QgsVectorLayer(f"Point?crs=EPSG:{epsg}", "SIRENE_raw", "memory")
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

        features = []
        for i in range(len(sirets)):
            # ── Filtre : exclure sections T (97-98) et U (99) ────────────────
            naf = nafs[i] or ""
            if naf and naf[:2].isdigit() and int(naf[:2]) >= 97:
                continue

            # ── Coordonnées (jointure sur le SIRET) ───────────────────────────
            xy = coords.get(sirets[i])
            if not xy:
                continue
            x, y = xy
            if not x or not y:
                continue
            try:
                xf, yf = float(x), float(y)
            except (ValueError, TypeError):
                continue
            if xf <= 0 or yf <= 0:
                continue

            # ── Nom ───────────────────────────────────────────────────────────
            nom = (enseignes[i] or "").strip() or (denoms[i] or "").strip()
            if not nom:
                continue

            adresse = " ".join(
                filter(
                    None,
                    [nums[i], reps[i], types_voie[i], libelles[i], cps[i], communes[i]],
                )
            ).strip()

            feat = QgsFeature(mem_layer.fields())
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(xf, yf)))
            feat.setAttribute("siret", sirets[i] or "")
            feat.setAttribute("nom", nom[:254])
            feat.setAttribute("activitePrincipaleEtablissement", naf)
            feat.setAttribute("adresse", adresse[:254])
            features.append(feat)

        pr.addFeatures(features)
        mem_layer.updateExtents()
        feedback.pushInfo(f"   ✓  {len(features)} établissement(s)")

        if not features:
            feedback.pushWarning("⚠  Aucun établissement localisé sur la commune")
            return None

        # ── Reprojection vers 2154 si besoin (DOM), puis découpage commune ────
        mem_layer.dataProvider().createSpatialIndex()
        if epsg != "2154":
            mem_layer = processing.run(
                "native:reprojectlayer",
                {"INPUT": mem_layer, "TARGET_CRS": "EPSG:2154", "OUTPUT": "memory:"},
                feedback=feedback,
            )["OUTPUT"]
        clipped = processing.run(
            "native:clip",
            {"INPUT": mem_layer, "OVERLAY": boundary_layer, "OUTPUT": "memory:"},
            feedback=feedback,
        )["OUTPUT"]
        clipped.setName("Établissements SIRENE")
        return clipped

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
        if style_key.startswith("rpg_"):
            getattr(self, f"_apply_{style_key}_style")(layer)
            return
        if style_key == "roads":
            self._apply_roads_style(layer)
            return
        if style_key == "railways":
            self._apply_railways_style(layer)
            return
        if style_key == "courbe_de_niveau":
            self._apply_courbe_de_niveau_style(layer)
            return
        if style_key == "vegetation":
            self._apply_vegetation_style(layer)
            return
        if style_key == "water_surface":
            self._apply_water_surface_style(layer)
            return
        if style_key == "rivers":
            self._apply_rivers_style(layer)
            return
        if style_key == "bdforet":
            self._apply_bdforet_style(layer)
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
            # Bâti : gris foncé, sans contour — contraste marqué avec les parcelles (#e0e0e0)
            "buildings": lambda: QgsFillSymbol.createSimple(
                {
                    "color": "#999999",
                    "outline_style": "no",
                }
            ),
        }

        factory = style_factories.get(style_key)
        if factory:
            layer.setRenderer(QgsSingleSymbolRenderer(factory()))
            layer.triggerRepaint()

    def _apply_roads_style(self, layer: QgsVectorLayer):
        """
        Voirie — rendu règle par règle (QgsRuleBasedRenderer), premier filtre gagnant.
        Utilise _make_line_rule (module-level) — voir aussi _apply_railways_style.

        Priorité :
          1. nature = 'Type autoroutier' / 'Bretelle'  (identification par nature)
          2. importance '1' à '5'                       (identification par importance)
          3. nature = 'Route empierrée' / 'Piste cyclable' / 'Chemin' / 'Sentier'
        Invisible : importance = '6', nature IN ('Escalier', 'Bac ou liaison maritime').

        Les filtres importance (règles 3-7) excluent explicitement les natures gérées
        séparément pour éviter les doubles rendus.  Les filtres nature (règles 8-10)
        excluent importance 1-6 : une route empierrée cotée ≤ 5 est rendue par sa
        règle d'importance (premier gagnant), cotée 6 elle reste invisible.
        """

        # Natures exclues des règles importance pour éviter les doubles rendus
        _X = (
            '"nature" NOT IN ('
            "'Type autoroutier','Bretelle','Escalier','Bac ou liaison maritime'"
            ")"
        )
        # Valeurs importance déjà traitées (ou à masquer) — exclues des règles nature
        _NI = "\"importance\" NOT IN ('1','2','3','4','5','6')"

        rules = [
            # ── Nature prioritaire ────────────────────────────────────────────
            # Rouge brique saturé : autoroutes bien distinctes, large trait lisible
            (
                "Autoroute",
                "\"nature\" = 'Type autoroutier'",
                "#D94020",
                0.8,
                Qt.SolidLine,
            ),
            # Orange vif : bretelles identifiables, trait fin
            ("Bretelle", "\"nature\" = 'Bretelle'", "#E06830", 0.35, Qt.SolidLine),
            # ── Importance : dégradé ambré-brun (plus fin = plus sombre pour rester lisible)
            (
                "Route imp. 1",
                f"\"importance\" = '1' AND {_X}",
                "#E89020",
                0.55,
                Qt.SolidLine,
            ),
            (
                "Route imp. 2",
                f"\"importance\" = '2' AND {_X}",
                "#D88028",
                0.4,
                Qt.SolidLine,
            ),
            (
                "Route imp. 3",
                f"\"importance\" = '3' AND {_X}",
                "#C07030",
                0.3,
                Qt.SolidLine,
            ),
            (
                "Route imp. 4",
                f"\"importance\" = '4' AND {_X}",
                "#A86028",
                0.2,
                Qt.SolidLine,
            ),
            (
                "Route imp. 5",
                f"\"importance\" = '5' AND {_X}",
                "#8C4C20",
                0.15,
                Qt.SolidLine,
            ),
            # ── Nature secondaire ─────────────────────────────────────────────
            # Brun terre : routes non revêtues, tirets
            (
                "Empierrée",
                f"\"nature\" = 'Route empierrée' AND {_NI}",
                "#7A5030",
                0.15,
                Qt.DashLine,
            ),
            # Vert forêt : convention française pistes cyclables, pointillés
            (
                "Piste cyclable",
                f"\"nature\" = 'Piste cyclable' AND {_NI}",
                "#2E8840",
                0.15,
                Qt.DotLine,
            ),
            # Sienne foncé : chemins pédestres, pointillés, le plus fin
            (
                "Chemin/Sentier",
                f"\"nature\" IN ('Chemin','Sentier') AND {_NI}",
                "#6A4820",
                0.1,
                Qt.DotLine,
            ),
        ]

        _apply_line_rules(layer, rules)

    def _apply_railways_style(self, layer: QgsVectorLayer):
        """Voie ferrée — rendu règle par règle (QgsRuleBasedRenderer)."""
        rules = [
            # Famille violet-indigo, en écho aux marqueurs de stations existants
            # (#AB47BC tramway, #7B1FA2 métro) — cohérence point/ligne garantie.
            # Indigo nuit : LGV, trait large très lisible
            ("LGV", "\"nature\" = 'LGV'", "#1C0878", 0.6, Qt.SolidLine),
            # Indigo-violet moyen : grande ligne classique
            (
                "Voie ferrée",
                "\"nature\" = 'Voie ferrée principale'",
                "#4030A0",
                0.45,
                Qt.SolidLine,
            ),
            # Violet vif : fait écho au marqueur de station tramway (#AB47BC)
            ("Tramway", "\"nature\" = 'Tramway'", "#9838B0", 0.3, Qt.SolidLine),
            # Violet profond : fait écho au marqueur de station métro (#7B1FA2)
            ("Métro", "\"nature\" = 'Métro'", "#701890", 0.3, Qt.SolidLine),
            # Lavande pâle : voie secondaire, tirets discrets
            (
                "Voie de service",
                "\"nature\" = 'Voie de service'",
                "#A878C8",
                0.15,
                Qt.DashLine,
            ),
            # Ardoise-violet : funiculaire, distinct des autres
            (
                "Funiculaire",
                "\"nature\" = 'Funiculaire ou crémaillère'",
                "#6A50A8",
                0.2,
                Qt.SolidLine,
            ),
        ]

        _apply_line_rules(layer, rules)

    # =========================================================================
    # Helper — Topographie
    # =========================================================================

    def _load_topo_wfs(self, bbox, boundary_layer, crs_2154, feedback):
        """Courbes de niveau IGN 5 m via WFS Géoplateforme."""
        return self._load_wfs_layer(
            "ELEVATION.CONTOUR.LINE:courbe",
            "Courbes de niveau (IGN 5 m)",
            bbox,
            boundary_layer,
            crs_2154,
            feedback,
        )

    def _load_topo_lidar(
        self, bbox, boundary_layer, crs_2154, interval, z_factor, azimuth, feedback
    ):
        """
        Télécharge le MNT LiDAR HD via une seule requête WMS sur le bbox communal,
        génère les courbes de niveau à l'intervalle demandé et un ombrage du relief
        (hillshade) en mode Superposition pour donner du relief au fond de plan.

        Retourne (contour_layer, hillshade_layer). hillshade_layer est None si
        la génération échoue.
        """
        import math
        import tempfile

        width_m = bbox.xMaximum() - bbox.xMinimum()
        height_m = bbox.yMaximum() - bbox.yMinimum()

        # Résolution : au moins 2 pixels par intervalle, au minimum 0.5 m (natif LiDAR HD).
        # Plafond à 3 000 px sur le grand côté pour éviter les rasters trop lourds.
        res_needed = max(interval / 2.0, 0.5)
        res_from_cap = max(width_m, height_m) / 3000.0
        res = max(res_needed, res_from_cap)

        px_w = max(1, int(math.ceil(width_m / res)))
        px_h = max(1, int(math.ceil(height_m / res)))

        wms_url = (
            "https://data.geopf.fr/wms-r"
            "?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
            "&LAYERS=IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"
            "&FORMAT=image/geotiff&STYLES="
            "&CRS=EPSG:2154"
            f"&BBOX={bbox.xMinimum()},{bbox.yMinimum()},{bbox.xMaximum()},{bbox.yMaximum()}"
            f"&WIDTH={px_w}&HEIGHT={px_h}"
        )

        tmpdir = tempfile.mkdtemp(prefix="fdp_lidar_")
        raster_path = os.path.join(tmpdir, "mnt.tif")

        feedback.pushInfo(f"   ⬇  MNT {px_w}×{px_h} px ({res:.1f} m/px)…")
        try:
            with requests.get(wms_url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(raster_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        if feedback.isCanceled():
                            return None, None
                        f.write(chunk)
        except Exception as exc:
            feedback.pushWarning(f"   ⚠  Téléchargement MNT : {exc}")
            return None, None

        # ── Hillshade ────────────────────────────────────────────────────────
        hillshade_path = os.path.join(tmpdir, "hillshade.tif")
        feedback.pushInfo(f"   🌄  Hillshade (Z×{z_factor}, az.{azimuth}°)…")
        try:
            processing.run(
                "gdal:hillshade",
                {
                    "INPUT": raster_path,
                    "BAND": 1,
                    "Z_FACTOR": z_factor,
                    "AZIMUTH": azimuth,
                    "ALTITUDE": 45,
                    "SCALE": 1.0,
                    "COMBINED": False,
                    "MULTIDIRECTIONAL": False,
                    "OUTPUT": hillshade_path,
                },
                feedback=feedback,
            )
            hillshade_rl = QgsRasterLayer(hillshade_path, "Ombrage du relief")
            if not hillshade_rl.isValid():
                hillshade_rl = None
        except Exception as exc:
            feedback.pushWarning(f"   ⚠  Hillshade : {exc}")
            hillshade_rl = None

        # Découpe du hillshade sur la limite communale : en import multi-communes,
        # les rasters bbox de communes voisines se chevauchent et leurs modes de
        # fusion se cumulent. Bande alpha → transparent hors commune.
        # En cas d'échec on garde le hillshade non découpé plutôt que rien.
        if hillshade_rl is not None:
            hillshade_clip_path = os.path.join(tmpdir, "hillshade_clip.tif")
            try:
                processing.run(
                    "gdal:cliprasterbymasklayer",
                    {
                        "INPUT": hillshade_path,
                        "MASK": boundary_layer,
                        "SOURCE_CRS": None,
                        "TARGET_CRS": None,
                        "NODATA": None,
                        "ALPHA_BAND": True,
                        "CROP_TO_CUTLINE": True,
                        "KEEP_RESOLUTION": True,
                        "MULTITHREADING": False,
                        "OUTPUT": hillshade_clip_path,
                    },
                    feedback=feedback,
                )
                clipped_rl = QgsRasterLayer(hillshade_clip_path, "Ombrage du relief")
                if clipped_rl.isValid():
                    hillshade_rl = clipped_rl
                else:
                    feedback.pushWarning(
                        "   ⚠  Découpe du hillshade invalide — raster bbox conservé."
                    )
            except Exception as exc:
                feedback.pushWarning(
                    f"   ⚠  Découpe du hillshade : {exc} — raster bbox conservé."
                )

        # ── Courbes de niveau ─────────────────────────────────────────────────
        contour_path = os.path.join(tmpdir, "contours.gpkg")
        processing.run(
            "gdal:contour",
            {
                "INPUT": raster_path,
                "BAND": 1,
                "INTERVAL": interval,
                "FIELD_NAME": "altitude",
                "OUTPUT": contour_path,
            },
            feedback=feedback,
        )

        raw = QgsVectorLayer(contour_path, "tmp", "ogr")
        if not raw.isValid():
            feedback.pushWarning("   ⚠  Courbes de niveau invalides.")
            return None, hillshade_rl

        raw.dataProvider().createSpatialIndex()
        clip_ctx = QgsProcessingContext()
        clip_ctx.setInvalidGeometryCheck(QgsFeatureRequest.GeometrySkipInvalid)
        clipped = processing.run(
            "native:clip",
            {"INPUT": raw, "OVERLAY": boundary_layer, "OUTPUT": "memory:"},
            context=clip_ctx,
            feedback=feedback,
        )["OUTPUT"]

        # Champ importance : courbe principale toutes les 5 × interval mètres.
        # Toutes les valeurs sont envoyées en un seul appel dataProvider pour éviter
        # N transactions séparées (critique sur les communes denses à petit intervalle).
        major = interval * 5
        clipped.dataProvider().addAttributes([QgsField("importance", QMetaType.Type.QString)])
        clipped.updateFields()
        imp_idx = clipped.fields().indexOf("importance")
        changes = {}
        for feat in clipped.getFeatures():
            try:
                alt = float(feat["altitude"])
                val = "1" if round(alt % major, 4) < 0.01 else "0"
            except (TypeError, ValueError):
                val = "0"
            changes[feat.id()] = {imp_idx: val}
        clipped.dataProvider().changeAttributeValues(changes)

        clipped.setName(f"Courbes de niveau LiDAR HD ({interval:g} m)")
        return clipped, hillshade_rl

    def _apply_courbe_de_niveau_style(self, layer: QgsVectorLayer):
        """
        Courbes de niveau — rendu règle par règle (QgsRuleBasedRenderer).

        Source : ELEVATION.CONTOUR.LINE:courbe (Géoplateforme IGN).
        Attributs utilisés :
          - importance : '1' = courbe principale (maîtresse), '0' = courbe secondaire
          - altitude   : réel (mètres NGF)
        """

        rules = [
            ("Courbe principale", "\"importance\" = '1'", "#000000", 0.22),
            ("Courbe secondaire", "\"importance\" = '0'", "#000000", 0.09),
            ("Autre", "ELSE", "#000000", 0.09),
        ]
        _apply_line_rules(layer, rules)

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
        _FORMATION_CODES = "'85.51Z','85.52Z','85.53Z','85.59A','85.59B','85.60Z'"
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
            (
                "Équipements & services publics",
                [(84, 84)],
                "#C1121F",
                3.0,
                "pentagon",
                None,
            ),
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
            (
                "Agriculture, sylviculture & pêche",
                [(1, 3)],
                "#2D6A4F",
                2.5,
                "triangle",
                None,
            ),
        ]

        # ── Source unique de vérité : la couleur vient de SIRENE_CATEGORIES ────
        # (les couleurs des remplissages « Bâti par activité »). Les pastilles
        # ne gardent en propre que forme + taille, et reprennent la couleur de
        # leur catégorie. Empêche toute dérive entre remplissages et pastilles ;
        # le fallback (couleur du tuple) protège si un libellé venait à différer.
        _cat_color = {c["label"]: c["color"] for c in SIRENE_CATEGORIES}
        groups = [
            (label, ranges, _cat_color.get(label, color), size, shape, expr)
            for (label, ranges, color, size, shape, expr) in groups
        ]

        # Déplacement data-défini : présent quand la couche vient de
        # build_displaced_sirene_layer (champs offset_x_mm / offset_y_mm).
        field_names = [f.name() for f in layer.fields()]
        use_dd_offset = "offset_x_m" in field_names

        def _make_sym(color, size, shape):
            sym = QgsMarkerSymbol.createSimple(
                {
                    "color": color,
                    "name": shape,
                    "size": str(size),
                    "outline_style": "no",
                }
            )
            if use_dd_offset:
                sl = sym.symbolLayer(0)
                # Offset en unités carte (mètres EPSG:2154) → le cercle grandit
                # naturellement avec le zoom, épousant l'échelle des bâtiments.
                sl.setOffsetUnit(QgsUnitTypes.RenderMapUnits)
                sl.setDataDefinedProperty(
                    QgsSymbolLayer.Property.PropertyOffset,
                    QgsProperty.fromExpression('array("offset_x_m", "offset_y_m")'),
                )
            return sym

        root_rule = QgsRuleBasedRenderer.Rule(None)

        for label, ranges, color, size, shape, custom_expr in groups:
            rule = QgsRuleBasedRenderer.Rule(_make_sym(color, size, shape))
            rule.setFilterExpression(
                custom_expr if custom_expr is not None else self._naf_div_expr(ranges)
            )
            rule.setLabel(label)
            root_rule.appendChild(rule)

        # Règle de repli (codes absents, malformés ou NAF inconnu)
        other_rule = QgsRuleBasedRenderer.Rule(_make_sym("#BBBBBB", 1.5, "circle"))
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
        categories = [
            ("Science et enseignement", "#FFF0B3"),
            ("Santé", "#B3F5E6"),
            ("Administratif ou militaire", "#F5B3B6"),
            ("Industriel et commercial", "#E8D5C4"),
            ("Culture et loisirs", "#B3DFF0"),
            ("Sport", "#FCDEC4"),
            ("Religieux", "#E0D0E8"),
            ("Gestion des eaux", "#C4E3F5"),
        ]
        rules = [
            (label, color, f"\"categorie\" = '{label}'")
            for label, color in categories
        ]
        _apply_fill_rules(
            layer, rules,
            fallback_color="#E8E8E8",
            fallback_label="Autre / non classé",
            symbol_props={"outline_color": "#888888", "outline_width": "0.2"},
        )

    # =========================================================================
    # Helpers – symbologie végétation & hydrographie
    # =========================================================================

    def _apply_vegetation_style(self, layer: QgsVectorLayer):
        """
        Végétation BDTOPO — une règle par valeur de 'nature' (toutes les
        catégories restent accessibles dans la légende et filtrables), mais
        seulement DEUX teintes à l'écran : végétation arborée (vert actuel du
        fond de plan) et végétation basse/cultivée (vert-jaune pâle). Le rendu
        global reste donc aussi discret qu'avant la catégorisation.
        Les natures forestières sont absentes de la couche : retirées au
        chargement (_VEGETATION_NATURES_EXCLUES) car couvertes par la
        BD Forêt V2.
        """
        _TREE = "#9BD79B"   # vert existant (ancien remplissage unique)
        _LOW = "#C6E0A5"    # vert-jaune pâle : haies, vignes, vergers…
        natures = [
            ("Bois", _TREE),
            ("Haie", _LOW),
            ("Verger", _LOW),
            ("Vigne", _LOW),
        ]
        rules = [
            (label, color, f"\"nature\" = '{label}'")
            for label, color in natures
        ]
        _apply_fill_rules(
            layer, rules,
            fallback_color=_LOW,
            fallback_label="Autre végétation",
            symbol_props={"outline_style": "no"},
        )

    def _apply_water_surface_style(self, layer: QgsVectorLayer):
        """
        Surfaces hydrographiques BDTOPO — catégorisation sur 'nature' +
        'persistance' (valeurs vérifiées sur le WFS : Permanent/Intermittent).
        Palette : famille de bleus proche de l'ancien remplissage unique ;
        l'eau intermittente ressort en bleu très pâle.
        """
        # Exclusion partagée : l'intermittent est traité par sa propre règle.
        # coalesce → une persistance NULL est traitée comme permanente.
        _P = "coalesce(\"persistance\", '') <> 'Intermittent'"
        rules = [
            (
                "Eau intermittente",
                "#C4E0EE",
                "coalesce(\"persistance\", '') = 'Intermittent'",
            ),
            (
                "Écoulement naturel",
                "#78BEDC",
                f"\"nature\" = 'Ecoulement naturel' AND {_P}",
            ),
            (
                "Plan d'eau / mare",
                "#8FCAE4",
                "\"nature\" IN ('Plan d''eau', 'Mare', 'Plan d''eau de gravière', "
                f"'Lagune', 'Estuaire') AND {_P}",
            ),
            (
                "Retenue / bassin",
                "#6FB0D0",
                "\"nature\" IN ('Retenue', 'Réservoir-bassin', "
                f"'Réservoir-bassin d''orage') AND {_P}",
            ),
        ]
        _apply_fill_rules(
            layer, rules,
            fallback_color="#78BEDC",
            fallback_label="Autre surface en eau",
            symbol_props={"outline_style": "no"},
        )

    def _apply_rivers_style(self, layer: QgsVectorLayer):
        """
        Cours d'eau BDTOPO — le champ booléen 'caractere_permanent' distingue
        les cours d'eau intermittents (tiretés, bleu clair). La règle ELSE
        reprend l'ancien trait unique : tout champ absent ou mal typé retombe
        donc sur le rendu permanent.
        """
        rules = [
            (
                "Cours d'eau intermittent",
                "\"caractere_permanent\" = false",
                "#7EB8D8",
                0.5,
                Qt.DashLine,
            ),
            ("Cours d'eau", "ELSE", "#3A9BD5", 0.8),
        ]
        _apply_line_rules(layer, rules)

    def _apply_bdforet_style(self, layer: QgsVectorLayer):
        """
        BD Forêt V2 (formation_vegetale) — catégorisation sur 'tfv_g11', la
        généralisation en 11 postes fournie par l'IGN. Les LIKE absorbent les
        variantes de libellés (« Forêt fermée feuillus » vs « … de feuillus »).
        Palette de verts cohérente avec la végétation BDTOPO.
        """
        rules = [
            ("Forêt fermée de feuillus", "#8CCD8C",
             "\"tfv_g11\" LIKE 'Forêt fermée%feuillus'"),
            ("Forêt fermée de conifères", "#5FA878",
             "\"tfv_g11\" LIKE 'Forêt fermée%conifères'"),
            ("Forêt fermée mixte", "#74BC82",
             "\"tfv_g11\" LIKE 'Forêt fermée mixte'"),
            ("Forêt fermée sans couvert arboré", "#B0D8A0",
             "\"tfv_g11\" LIKE 'Forêt fermée sans couvert%'"),
            ("Forêt ouverte", "#BFE3AC",
             "\"tfv_g11\" LIKE 'Forêt ouverte%'"),
            ("Peupleraie", "#7FCDB5", "\"tfv_g11\" = 'Peupleraie'"),
            ("Lande", "#D8D2A0", "\"tfv_g11\" = 'Lande'"),
            ("Formation herbacée", "#E4E8B8",
             "\"tfv_g11\" = 'Formation herbacée'"),
        ]
        _apply_fill_rules(
            layer, rules,
            fallback_color="#C8E6C4",
            fallback_label="Autre formation végétale",
            symbol_props={"outline_style": "no"},
        )

    # =========================================================================
    # Helpers – symbologie RPG
    # =========================================================================

    def _apply_rpg_parcelles_style(self, layer: QgsVectorLayer):
        """
        Rendu règle-par-règle des parcelles agricoles RPG.
        Champ : code_cultu (xsd:string).
        Codes vérifiés sur RPG.LATEST:codes_cultures (147 entrées, 2025-03-04).
        Toutes les 146 entrées actives sont mappées ; ZZZ (culture inconnue)
        tombe dans le repli.
        Données dans _RPG_PARCELLES_GROUPS (module-level).
        """
        rules = []
        for label, codes, color in _RPG_PARCELLES_GROUPS:
            quoted = ", ".join(f"'{c}'" for c in codes)
            rules.append((label, color, f'"code_cultu" IN ({quoted})'))
        # ZZZ (culture inconnue) et tout code non répertorié → repli
        _apply_fill_rules(
            layer, rules,
            fallback_color="#E8D880",
            fallback_label="Culture non identifiée",
            symbol_props={"outline_style": "no"},
        )

    def _apply_rpg_ilots_style(self, layer: QgsVectorLayer):
        """Îlots anonymisés RPG — grille neutre, sans catégorisation."""
        sym = QgsFillSymbol.createSimple(
            {
                "color": "#EEE8C0",
                "outline_color": "#AAAAAA",
                "outline_width": "0.3",
            }
        )
        layer.setRenderer(QgsSingleSymbolRenderer(sym))
        layer.triggerRepaint()

    def _apply_rpg_pac_style(self, layer: QgsVectorLayer):
        """
        Rendu par catégorie PAC — champ cat_cult_p (xsd:string).
        Valeurs attendues : 'TA', 'PP', 'CP', 'SB' (codes officiels PAC).
        """
        pac = [
            ("TA", "Terres arables", "#F0D060"),
            ("CP", "Cultures permanentes", "#FF8C00"),
            ("PP", "Prairies permanentes", "#18A018"),
            ("SB", "Surfaces boisées", "#207030"),
        ]
        rules = [
            (label, color, f"\"cat_cult_p\" = '{code}'")
            for code, label, color in pac
        ]
        _apply_fill_rules(
            layer, rules,
            fallback_color="#E8E8E8",
            fallback_label="Autre / non classé",
            symbol_props={"outline_style": "no"},
        )

    def _apply_rpg_pp_style(self, layer: QgsVectorLayer):
        """Prairies permanentes RPG — remplissage vert uniforme."""
        sym = QgsFillSymbol.createSimple({"color": "#29A86A", "outline_style": "no"})
        layer.setRenderer(QgsSingleSymbolRenderer(sym))
        layer.triggerRepaint()

    def _apply_rpg_iae_style(self, layer: QgsVectorLayer):
        """
        Parcelles éligibles IAE — palette verte biodiversité sur code_cultu.
        La couche est déjà filtrée elig_iae=1 côté serveur ; on accentue
        les éléments à haute valeur écologique (prairies, boisements,
        légumineuses) et on atténue les cultures céréalières.
        """
        groups = [
            ("Prairies permanentes", ("PPH", "SPH", "SPL"), "#18A018"),
            (
                "Prairies & fourrages temporaires",
                (
                    "AFG",
                    "CPL",
                    "GRA",
                    "LOT",
                    "LUZ",
                    "MLC",
                    "MLF",
                    "MLG",
                    "PTR",
                    "SAI",
                    "TRE",
                    "VES",
                ),
                "#29A86A",
            ),
            (
                "Légumineuses & protéagineux",
                (
                    "FEV",
                    "FVL",
                    "FVP",
                    "GES",
                    "LDH",
                    "LDP",
                    "LEC",
                    "MPC",
                    "PAG",
                    "PCH",
                    "PHI",
                    "PHS",
                    "PPR",
                ),
                "#45C484",
            ),
            ("Surfaces boisées", ("CAE", "CEE", "CNA", "CNE", "SBO"), "#207030"),
            (
                "Céréales",
                (
                    "AVH",
                    "AVP",
                    "BDH",
                    "BDP",
                    "BTH",
                    "BTP",
                    "CAG",
                    "CAH",
                    "EPE",
                    "MCS",
                    "MCR",
                    "MID",
                    "MIS",
                    "MLT",
                    "MOH",
                    "ORH",
                    "ORP",
                    "RIZ",
                    "SGH",
                    "SGP",
                    "SOG",
                    "SRS",
                    "TTH",
                    "TTP",
                ),
                "#90DCA8",
            ),
        ]
        rules = []
        for label, codes, color in groups:
            quoted = ", ".join(f"'{c}'" for c in codes)
            rules.append((label, color, f'"code_cultu" IN ({quoted})'))
        _apply_fill_rules(
            layer, rules,
            fallback_color="#45C484",
            fallback_label="Autres éléments IAE",
            symbol_props={"outline_style": "no"},
        )

    def _apply_rpg_zdh_style(self, layer: QgsVectorLayer):
        """
        Zones de densité homogène (ZDH) — dégradé sur le champ prorata.
        prorata (xsd:string) représente la proportion de surface agricole
        dans la zone ; converti en réel avec to_real() dans l'expression.
        """
        zdh = [
            ('to_real("prorata") >= 0.8', "ZDH — forte densité", "#D87020"),
            ('to_real("prorata") >= 0.5', "ZDH — densité moyenne", "#D4B060"),
        ]
        rules = [(label, color, expr) for expr, label, color in zdh]
        _apply_fill_rules(
            layer, rules,
            fallback_color="#F0EAD6",
            fallback_label="ZDH — faible densité / indéterminé",
            symbol_props={"outline_style": "no"},
        )

    # =========================================================================
    # Helper – symbologie personnalisée (depuis _LayerSelectorDialog)
    # =========================================================================

    def _apply_custom_style(self, layer: QgsVectorLayer, style: dict, geom_type: str):
        """
        Applique un style issu du dialogue _LayerSelectorDialog.
        Le dict 'style' utilise des QColor avec canal alpha pour l'opacité.
        Les valeurs de largeur/taille sont en mm (float).
        """
        # Convertit le style QGIS "outline_style" en valeur attendue par
        # QgsFillSymbol / QgsLineSymbol : "solid", "dash", "no".
        _outline_map = {"solid": "solid", "dashed": "dash", "none": "no"}
        _line_map = {"solid": "solid", "dashed": "dash"}

        if geom_type == "polygon":
            fc = style.get("fill_color", QColor(200, 200, 200, 255))
            oc = style.get("outline_color", QColor("#000000"))
            ow = style.get("outline_width", 0.3)
            os_ = _outline_map.get(style.get("outline_style", "none"), "no")
            # Encode RGBA pour que QGIS respecte l'opacité du remplissage
            color_str = f"{fc.red()},{fc.green()},{fc.blue()},{fc.alpha()}"
            sym = QgsFillSymbol.createSimple(
                {
                    "color": color_str,
                    "outline_color": oc.name(),
                    "outline_width": str(ow),
                    "outline_style": os_,
                }
            )

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
            sym = QgsMarkerSymbol.createSimple(
                {
                    "color": mc.name(),
                    "name": "circle",
                    "size": str(ms),
                    "outline_style": "no",
                }
            )

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
        self.setWindowTitle("Commune")
        self.setMinimumWidth(400)
        self.selected_commune = None
        self._features = features

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(f"{len(features)} résultat(s) — sélectionnez la commune :")
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
        self.setWindowTitle("Couches du fond de plan")
        self.setMinimumSize(1000, 640)
        self.result_layers = []
        self.topo_config = None

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

        root_layout.addWidget(self._build_topo_section())

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

        self._build_section(
            scroll_layout,
            "Zonages de protection",
            "zones",
            collapsible=True,
            checkbox_all=True,
            insert_at_top=True,
            note="Périmètres réglementaires et d'inventaire (Natura 2000, ZNIEFF, "
            "APB, PNR), rendus au-dessus du fond de plan.",
        )
        self._build_section(scroll_layout, "Fond de plan", "default", collapsible=False)
        self._build_section(
            scroll_layout,
            "Données thématiques",
            "extra",
            collapsible=True,
            checkbox_all=True,
        )
        self._build_section(
            scroll_layout,
            "Agriculture",
            "rural",
            collapsible=True,
            checkbox_all=True,
            note="Recommandé pour les communes rurales.",
        )
        scroll_layout.addStretch()

        scroll.setWidget(scroll_content)
        left_layout.addWidget(scroll)
        splitter.addWidget(left_outer)

        # ── Panneau droit : ordre + éditeur de style ──────────────────────────
        right_outer = QWidget()
        right_layout = QVBoxLayout(right_outer)
        splitter.addWidget(right_outer)

        # Liste d'ordre
        order_group = QGroupBox("Ordre des couches  ↑ haut = premier plan")
        order_vbox = QVBoxLayout(order_group)

        self._order_list = QListWidget()
        self._order_list.setDragDropMode(QAbstractItemView.InternalMove)
        order_vbox.addWidget(self._order_list)

        arrow_layout = QHBoxLayout()
        self._btn_up = QPushButton("▲ Monter")
        self._btn_down = QPushButton("▼ Descendre")
        arrow_layout.addWidget(self._btn_up)
        arrow_layout.addWidget(self._btn_down)
        order_vbox.addLayout(arrow_layout)
        right_layout.addWidget(order_group, stretch=2)

        # Éditeur de style
        self._style_group = QGroupBox("Style de la couche sélectionnée")
        self._style_vbox = QVBoxLayout(self._style_group)
        # _style_content est le seul enfant direct de _style_vbox.
        # On le remplace en entier (replaceWidget) plutôt que de modifier son
        # contenu widget par widget — cela évite le clignotement causé par
        # deleteLater() qui est asynchrone et laisse les anciens widgets visibles
        # le temps que le prochain tour d'event loop les supprime.
        self._style_content = QWidget()
        QVBoxLayout(self._style_content).addWidget(
            QLabel("← Sélectionnez une couche pour éditer son style.")
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

    def _build_topo_section(self):
        """Panneau fixe affiché au-dessus de tout — choix de la source topographique."""
        group = QGroupBox("Topographie")
        hbox = QHBoxLayout(group)
        hbox.setContentsMargins(8, 4, 8, 4)

        self._topo_none = QRadioButton("Aucune")
        self._topo_wfs = QRadioButton("Courbes IGN  (5 m, WFS)")
        self._topo_lidar = QRadioButton("LiDAR HD  — courbes générées")
        self._topo_none.setChecked(True)

        self._topo_interval = QDoubleSpinBox()
        self._topo_interval.setRange(0.5, 50.0)
        self._topo_interval.setSingleStep(0.5)
        self._topo_interval.setDecimals(1)
        self._topo_interval.setSuffix(" m")
        self._topo_interval.setValue(5.0)
        self._topo_interval.setEnabled(False)
        self._topo_interval.setFixedWidth(72)

        self._topo_zfactor = QDoubleSpinBox()
        self._topo_zfactor.setRange(0.5, 5.0)
        self._topo_zfactor.setSingleStep(0.1)
        self._topo_zfactor.setDecimals(1)
        self._topo_zfactor.setPrefix("Z×")
        self._topo_zfactor.setValue(1.5)
        self._topo_zfactor.setEnabled(False)
        self._topo_zfactor.setFixedWidth(72)

        self._topo_azimuth = QSpinBox()
        self._topo_azimuth.setRange(0, 360)
        self._topo_azimuth.setSingleStep(15)
        self._topo_azimuth.setSuffix("°")
        self._topo_azimuth.setValue(315)
        self._topo_azimuth.setEnabled(False)
        self._topo_azimuth.setFixedWidth(64)

        def _on_lidar_toggled(checked):
            self._topo_interval.setEnabled(checked)
            self._topo_zfactor.setEnabled(checked)
            self._topo_azimuth.setEnabled(checked)

        self._topo_lidar.toggled.connect(_on_lidar_toggled)

        hbox.addWidget(self._topo_none)
        hbox.addSpacing(16)
        hbox.addWidget(self._topo_wfs)
        hbox.addSpacing(16)
        hbox.addWidget(self._topo_lidar)
        hbox.addSpacing(6)
        hbox.addWidget(QLabel("intervalle :"))
        hbox.addWidget(self._topo_interval)
        hbox.addSpacing(6)
        hbox.addWidget(QLabel("relief :"))
        hbox.addWidget(self._topo_zfactor)
        hbox.addSpacing(4)
        hbox.addWidget(QLabel("az."))
        hbox.addWidget(self._topo_azimuth)
        hbox.addStretch()
        return group

    def _build_section(
        self,
        parent_layout,
        title,
        section,
        collapsible,
        note=None,
        checkbox_all=False,
        insert_at_top=False,
    ):
        """
        Construit un QGroupBox avec les checkboxes de la section donnée.
        `insert_at_top` (avec checkbox_all) : le toggle insère les couches en
        TÊTE de la liste d'ordre (rendu au-dessus de tout) au lieu de la fin —
        utilisé par les zonages de protection.
        """
        entries = [e for e in _LAYER_CATALOGUE if e["section"] == section]
        group = QGroupBox(title)
        group_vbox = QVBoxLayout(group)

        if checkbox_all:
            # Case unique dans l'en-tête — cocher active toutes les couches
            # du groupe d'un coup (pas de case individuelle par couche).
            group.setCheckable(True)
            group.setChecked(False)
            container = QWidget()
            container_vbox = QVBoxLayout(container)
            container_vbox.setContentsMargins(0, 0, 0, 0)
            if note:
                note_lbl = QLabel(note)
                note_lbl.setStyleSheet("color: #888888; font-size: 9pt;")
                note_lbl.setWordWrap(True)
                container_vbox.addWidget(note_lbl)
            for entry in entries:
                lbl = QLabel("• " + entry["display_name"])
                lbl.setStyleSheet("color: #555555; font-size: 9pt;")
                container_vbox.addWidget(lbl)
            group_vbox.addWidget(container)
            container.setVisible(False)
            group.toggled.connect(container.setVisible)

            def _on_group_toggled(checked, _entries=entries, _top=insert_at_top):
                # insert_at_top : les entrées gardent l'ordre du catalogue en
                # tête de liste (lignes 0..n) ; sinon ajout en fin (comportement
                # historique des sections Agriculture / Données thématiques).
                for i, e in enumerate(_entries):
                    if checked:
                        if not any(
                            self._order_list.item(j).data(Qt.UserRole) == e["style_key"]
                            for j in range(self._order_list.count())
                        ):
                            self._add_to_order(e, row=i if _top else None)
                    else:
                        self._remove_from_order(e["style_key"])

            group.toggled.connect(_on_group_toggled)
            parent_layout.addWidget(group)
            return

        if collapsible:
            # Technique collapse : QGroupBox checkable + conteneur masquable.
            # Quand le groupe est décoché, le conteneur est masqué → la boîte
            # se réduit à sa seule barre de titre.
            group.setCheckable(True)
            group.setChecked(False)  # fermé par défaut
            container = QWidget()
            container_vbox = QVBoxLayout(container)
            container_vbox.setContentsMargins(0, 0, 0, 0)
            if note:
                note_lbl = QLabel(note)
                note_lbl.setStyleSheet("color: #888888; font-size: 9pt;")
                note_lbl.setWordWrap(True)
                container_vbox.addWidget(note_lbl)
            for entry in entries:
                cb = self._make_checkbox(entry)
                container_vbox.addWidget(cb)
            group_vbox.addWidget(container)
            container.setVisible(False)
            group.toggled.connect(container.setVisible)
        else:
            if note:
                note_lbl = QLabel(note)
                note_lbl.setStyleSheet("color: #888888; font-size: 9pt;")
                note_lbl.setWordWrap(True)
                group_vbox.addWidget(note_lbl)
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

    def _add_to_order(self, entry, row=None):
        """Ajoute la couche en fin de liste, ou à la ligne `row` si fournie."""
        item = QListWidgetItem(entry["display_name"])
        item.setData(Qt.UserRole, entry["style_key"])
        if row is None:
            self._order_list.addItem(item)
        else:
            self._order_list.insertItem(row, item)

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

        _RULE_BASED_KEYS = {"sirene", "roads", "railways"}
        _RULE_BASED_LABELS = {
            "sirene": "Symbologie par catégorie NAF — automatique.",
            "roads": "Symbologie hiérarchique par nature et importance — automatique.",
            "railways": "Symbologie par type de voie — automatique.",
        }
        if sk in _RULE_BASED_KEYS:
            lay.addWidget(
                QLabel(
                    _RULE_BASED_LABELS.get(sk, "Rendu par règles — non modifiable ici.")
                )
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
            lw.valueChanged.connect(lambda val, s=style: s.update({"line_width": val}))
            form.addRow("Épaisseur :", lw)

            ls_combo = QComboBox()
            ls_combo.addItems(["Plein", "Tirets"])
            ls_combo.setCurrentIndex(
                {"solid": 0, "dashed": 1}.get(style.get("line_style", "solid"), 0)
            )
            ls_combo.currentIndexChanged.connect(
                lambda idx, s=style: s.update({"line_style": ["solid", "dashed"][idx]})
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
            ms.valueChanged.connect(lambda val, s=style: s.update({"marker_size": val}))
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
        self._style_content.hide()  # masquage immédiat (synchrone)
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
        if self._topo_wfs.isChecked():
            self.topo_config = {"mode": "wfs"}
        elif self._topo_lidar.isChecked():
            self.topo_config = {
                "mode": "lidar",
                "interval": self._topo_interval.value(),
                "z_factor": self._topo_zfactor.value(),
                "azimuth": self._topo_azimuth.value(),
            }
        else:
            self.topo_config = None
        super().accept()
