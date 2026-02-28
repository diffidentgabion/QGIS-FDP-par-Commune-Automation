# -*- coding: utf-8 -*-
"""
FDP par Commune — Génération automatique d'un fond de plan communal
Script QGIS Processing Toolbox

Installation :
    Traitement > Options > Traitement > Scripts > Dossiers des scripts
    → pointer vers le dossier contenant ce fichier, puis recharger les fournisseurs.
"""

import json
import gzip
import csv
import os
import tempfile

import requests
from osgeo import ogr

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QListWidget, QDialogButtonBox,
    QLabel, QMessageBox, QFileDialog,
)
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterString,
    QgsVectorLayer,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsFeature,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsSingleSymbolRenderer,
)
import processing


# =============================================================================
# Algorithme principal
# =============================================================================

class FDPParCommune(QgsProcessingAlgorithm):
    """Charge automatiquement un fond de plan complet pour une commune française."""

    NOM_COMMUNE = 'NOM_COMMUNE'

    # ── Métadonnées Processing ────────────────────────────────────────────────

    def flags(self):
        # FlagNoThreading oblige QGIS à exécuter cet algorithme dans le thread
        # principal de Qt, ce qui est nécessaire pour afficher des boîtes de
        # dialogue Qt (QDialog, QMessageBox, QFileDialog) en toute sécurité.
        return super().flags() | QgsProcessingAlgorithm.FlagNoThreading

    def name(self):
        return 'fdp_par_commune'

    def displayName(self):
        return 'FDP par Commune'

    def group(self):
        return 'Fond de Plan'

    def groupId(self):
        return 'fond_de_plan'

    def shortHelpString(self):
        return (
            'Génère un fond de plan communal à partir de sources ouvertes :\n'
            '  • IGN Géoplateforme WFS (ADMIN EXPRESS, Cadastre, BD TOPO)\n'
            '  • Géo-SIRENE (établissements)\n\n'
            'Saisissez un nom de commune (recherche partielle acceptée).'
        )

    def createInstance(self):
        return FDPParCommune()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterString(
                self.NOM_COMMUNE,
                'Nom de la commune',
                defaultValue='',
            )
        )

    # ── Point d'entrée ────────────────────────────────────────────────────────

    def processAlgorithm(self, parameters, context, feedback):

        # ── 1. Recherche et sélection de la commune ──────────────────────────
        nom_input = self.parameterAsString(parameters, self.NOM_COMMUNE, context)
        feedback.pushInfo(f'Recherche de la commune : {nom_input}…')

        commune = self._search_commune(nom_input, feedback)
        if commune is None:
            raise Exception('Aucune commune sélectionnée. Traitement annulé.')

        nom   = commune['nom']
        insee = commune['code']
        dep   = self._get_dep(insee)
        feedback.pushInfo(f'Commune : {nom}  |  INSEE : {insee}  |  Département : {dep}')
        self.setProgress(5)

        # ── 2. Géométrie communale reprojetée en EPSG:2154 ───────────────────
        crs_4326 = QgsCoordinateReferenceSystem('EPSG:4326')
        crs_2154 = QgsCoordinateReferenceSystem('EPSG:2154')
        xform    = QgsCoordinateTransform(crs_4326, crs_2154, QgsProject.instance())

        # Le contour renvoyé par l'API Géo est en GeoJSON / EPSG:4326
        commune_geom = self._geojson_to_qgsgeometry(commune['geometry'])
        commune_geom.transform(xform)
        bbox = commune_geom.boundingBox()
        feedback.pushInfo(f'Emprise Lambert 93 : {bbox.toString(0)}')

        # Couche limite unique réutilisée pour tous les découpages
        boundary_layer = self._geom_to_temp_layer(commune_geom, 'Polygon', crs_2154)
        self.setProgress(10)

        # ── 3. Couches WFS IGN Géoplateforme ─────────────────────────────────
        # (typename WFS, nom d'affichage, clé de style)
        wfs_definitions = [
            ('ADMINEXPRESS-COG-CARTO.LATEST:commune',
             'Commune (limite)', 'commune_boundary'),
            ('CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle',
             'Parcelles cadastrales', 'parcels'),
            ('BDTOPO_V3:cours_d_eau',
             "Hydrographie - cours d'eau", 'rivers'),
            ('BDTOPO_V3:surface_hydrographique',
             'Hydrographie - surface', 'water_surface'),
            ('BDTOPO_V3:troncon_de_route',
             'Voirie', 'roads'),
            ('BDTOPO_V3:voie_ferree',
             'Voie ferrée', 'railways'),
            ('BDTOPO_V3:batiment',
             'Bâti', 'buildings'),
            ('BDTOPO_V3:zone_de_vegetation',
             'Végétation', 'vegetation'),
        ]

        loaded_layers = {}                         # style_key → QgsVectorLayer
        progress_per_layer = 40 / len(wfs_definitions)

        for i, (typename, display_name, style_key) in enumerate(wfs_definitions):
            if feedback.isCanceled():
                return {}
            feedback.pushInfo(f'Chargement : {display_name}…')
            layer = self._load_wfs_layer(
                typename, display_name, bbox, boundary_layer, crs_2154, feedback
            )
            if layer:
                loaded_layers[style_key] = layer
            self.setProgress(10 + int((i + 1) * progress_per_layer))

        # ── 4. Établissements SIRENE ──────────────────────────────────────────
        if not feedback.isCanceled():
            feedback.pushInfo('Chargement des établissements SIRENE…')
            sirene_layer = self._load_sirene(
                dep, insee, boundary_layer, crs_4326, crs_2154, feedback
            )
            if sirene_layer:
                loaded_layers['sirene'] = sirene_layer
        self.setProgress(80)

        # ── 5. Groupe QGIS + symbologie + ajout des couches ──────────────────
        # Ordre d'ajout : bas → haut dans la légende
        layer_order = [
            'commune_boundary',
            'parcels',
            'water_surface',
            'rivers',
            'vegetation',
            'railways',
            'roads',
            'buildings',
            'sirene',
        ]

        root  = QgsProject.instance().layerTreeRoot()
        group = root.insertGroup(0, nom)   # groupe en tête de légende

        for style_key in layer_order:
            if style_key in loaded_layers:
                layer = loaded_layers[style_key]
                self._apply_style(layer, style_key)
                # addMapLayer(layer, False) : ajoute au projet sans le placer à
                # la racine de l'arbre — on l'insère manuellement dans le groupe.
                QgsProject.instance().addMapLayer(layer, False)
                group.addLayer(layer)

        feedback.pushInfo(
            f'{len(loaded_layers)} couche(s) chargée(s) dans le groupe « {nom} ».'
        )
        self.setProgress(90)

        # ── 6. Proposition d'enregistrement .qgz ─────────────────────────────
        reply = QMessageBox.question(
            None,
            'Enregistrer le projet',
            f'Fond de plan « {nom} » créé avec succès.\n\n'
            'Voulez-vous enregistrer le projet en fichier .qgz ?',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            default_filename = nom.replace(' ', '_') + '_basemap.qgz'
            path, _ = QFileDialog.getSaveFileName(
                None,
                'Enregistrer le projet QGIS',
                os.path.join(os.path.expanduser('~'), default_filename),
                'Projet QGIS (*.qgz)',
            )
            if path:
                QgsProject.instance().write(path)
                feedback.pushInfo(f'Projet enregistré : {path}')

        self.setProgress(100)
        feedback.pushInfo('Traitement terminé.')
        return {}

    # =========================================================================
    # Helper – recherche et sélection de commune
    # =========================================================================

    def _search_commune(self, nom_input, feedback):
        """
        Interroge l'API Géo gouv.fr et renvoie un dict commune, ou None si annulé.
        Le dict contient les clés : 'nom', 'code' (INSEE), 'geometry' (GeoJSON).
        """
        url = (
            'https://geo.api.gouv.fr/communes'
            f'?nom={requests.utils.quote(nom_input)}'
            '&fields=nom,code,contour&format=geojson&geometry=contour'
        )
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise Exception(f"Impossible de contacter l'API Géo : {e}")

        features = resp.json().get('features', [])
        if not features:
            raise Exception(f"Aucune commune trouvée pour « {nom_input} ».")

        if len(features) == 1:
            p = features[0]['properties']
            return {
                'nom':      p['nom'],
                'code':     p['code'],
                'geometry': features[0]['geometry'],
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
        if insee_code.startswith('2A'):
            return '2A'
        if insee_code.startswith('2B'):
            return '2B'
        if insee_code.startswith('97'):
            return insee_code[:3]   # ex. '974' → La Réunion
        return insee_code[:2]       # ex. '75' → Paris

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
        layer = QgsVectorLayer(f'{geom_type}?crs={crs.authid()}', '_boundary', 'memory')
        feat  = QgsFeature()
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
            f'{bbox.xMinimum()},{bbox.yMinimum()},'
            f'{bbox.xMaximum()},{bbox.yMaximum()},EPSG:2154'
        )
        uri = (
            'https://data.geopf.fr/wfs/ows'
            '?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature'
            f'&TYPENAME={typename}&SRSNAME=EPSG:2154&BBOX={bbox_str}'
        )

        layer = QgsVectorLayer(uri, display_name, 'WFS')

        if not layer.isValid():
            feedback.pushWarning(f'  ⚠ Couche WFS invalide : {display_name}')
            return None
        if layer.featureCount() == 0:
            feedback.pushWarning(f'  ⚠ Aucune entité retournée : {display_name}')
            return None

        # Découpage sur le contour communal
        try:
            clipped = processing.run('native:clip', {
                'INPUT':   layer,
                'OVERLAY': boundary_layer,
                'OUTPUT':  'memory:',
            })['OUTPUT']
            clipped.setName(display_name)
            return clipped
        except Exception as e:
            feedback.pushWarning(
                f'  ⚠ Erreur lors du découpage de {display_name} : {e} '
                f'— couche non découpée utilisée en remplacement'
            )
            return layer   # repli : couche non découpée plutôt que rien

    # =========================================================================
    # Helper – SIRENE
    # =========================================================================

    def _load_sirene(
        self,
        dep: str,
        insee: str,
        boundary_layer: QgsVectorLayer,
        crs_4326: QgsCoordinateReferenceSystem,
        crs_2154: QgsCoordinateReferenceSystem,
        feedback,
    ):
        """
        Télécharge le fichier Géo-SIRENE du département, filtre sur la commune,
        charge en couche point, reprojette en L93 et découpe sur le contour.
        Les fichiers temporaires sont supprimés dans tous les cas (finally).
        """
        url     = f'https://files.data.gouv.fr/geo-sirene/last/dep/geo_siret_{dep}.csv.gz'
        tmp_gz  = os.path.join(tempfile.gettempdir(), f'geo_siret_{dep}.csv.gz')
        tmp_csv = os.path.join(tempfile.gettempdir(), f'sirene_{insee}.csv')

        try:
            # ── Téléchargement en streaming ───────────────────────────────────
            feedback.pushInfo(
                f'  Téléchargement SIRENE dép. {dep} '
                f'(fichier potentiellement volumineux, patientez…)'
            )
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(tmp_gz, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=2 * 1024 * 1024):
                        f.write(chunk)

            # ── Filtrage à la volée pendant la décompression ──────────────────
            count = 0
            with gzip.open(tmp_gz, 'rt', encoding='utf-8', errors='replace') as gz_in, \
                 open(tmp_csv, 'w', newline='', encoding='utf-8') as csv_out:

                reader = csv.DictReader(gz_in)
                writer = None

                for row in reader:
                    if row.get('codecommuneetablissement') != insee:
                        continue
                    # Ignorer les lignes sans coordonnées géographiques
                    if not row.get('longitude') or not row.get('latitude'):
                        continue
                    if writer is None:
                        writer = csv.DictWriter(csv_out, fieldnames=reader.fieldnames)
                        writer.writeheader()
                    writer.writerow(row)
                    count += 1

            feedback.pushInfo(f'  {count} établissement(s) trouvé(s).')
            if count == 0:
                return None

            # ── Chargement CSV → couche point ─────────────────────────────────
            # Les coordonnées SIRENE sont en WGS84 (EPSG:4326)
            csv_uri = (
                f"file:///{tmp_csv.replace(os.sep, '/')}"
                '?delimiter=,&xField=longitude&yField=latitude&crs=EPSG:4326'
            )
            raw_layer = QgsVectorLayer(csv_uri, 'SIRENE_raw', 'delimitedtext')
            if not raw_layer.isValid():
                feedback.pushWarning('  ⚠ Couche SIRENE invalide après chargement.')
                return None

            # ── Reprojection EPSG:4326 → EPSG:2154 ───────────────────────────
            reprojected = processing.run('native:reprojectlayer', {
                'INPUT':      raw_layer,
                'TARGET_CRS': crs_2154,
                'OUTPUT':     'memory:',
            })['OUTPUT']

            # ── Découpage sur le contour communal ─────────────────────────────
            clipped = processing.run('native:clip', {
                'INPUT':   reprojected,
                'OVERLAY': boundary_layer,
                'OUTPUT':  'memory:',
            })['OUTPUT']

            clipped.setName('Établissements SIRENE')
            return clipped

        except requests.HTTPError as e:
            feedback.pushWarning(f'  ⚠ Impossible de télécharger SIRENE ({dep}) : {e}')
            return None
        except Exception as e:
            feedback.pushWarning(f'  ⚠ Erreur SIRENE : {e}')
            return None
        finally:
            # Nettoyage systématique des fichiers temporaires
            for path in (tmp_gz, tmp_csv):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass

    # =========================================================================
    # Helpers – symbologie
    # =========================================================================

    def _apply_style(self, layer: QgsVectorLayer, style_key: str):
        """
        Applique une symbologie par défaut cohérente pour un fond de plan
        architectural : tons neutres, palette minimale.
        """
        # Chaque entrée est un callable qui renvoie un QgsSymbol configuré.
        # On utilise des lambdas pour éviter de créer des symboles inutilisés.
        style_factories = {
            # Limite communale : contour noir fin, sans remplissage
            'commune_boundary': lambda: QgsFillSymbol.createSimple({
                'color':         '0,0,0,0',
                'outline_color': '#000000',
                'outline_width': '0.5',
            }),
            # Parcelles cadastrales : contour gris foncé très fin, sans remplissage
            'parcels': lambda: QgsFillSymbol.createSimple({
                'color':         '0,0,0,0',
                'outline_color': '#666666',
                'outline_width': '0.2',
            }),
            # Surfaces en eau : bleu clair, sans contour
            'water_surface': lambda: QgsFillSymbol.createSimple({
                'color':         '#aad3df',
                'outline_style': 'no',
            }),
            # Cours d'eau : ligne bleu moyen
            'rivers': lambda: QgsLineSymbol.createSimple({
                'color': '#6baed6',
                'width': '0.8',
            }),
            # Végétation : vert très pâle, sans contour
            'vegetation': lambda: QgsFillSymbol.createSimple({
                'color':         '#c8e6c4',
                'outline_style': 'no',
            }),
            # Voirie : ligne blanche (s'intègre au fond clair)
            'roads': lambda: QgsLineSymbol.createSimple({
                'color': '#ffffff',
                'width': '0.5',
            }),
            # Voie ferrée : tirets gris
            'railways': lambda: QgsLineSymbol.createSimple({
                'color':           '#666666',
                'width':           '0.7',
                'customdash':      '5;3',
                'use_custom_dash': '1',
            }),
            # Bâti : gris moyen, sans contour
            'buildings': lambda: QgsFillSymbol.createSimple({
                'color':         '#c0c0c0',
                'outline_style': 'no',
            }),
            # Points SIRENE : petit cercle sombre
            'sirene': lambda: QgsMarkerSymbol.createSimple({
                'color':         '#333333',
                'size':          '1.5',
                'outline_style': 'no',
            }),
        }

        factory = style_factories.get(style_key)
        if factory:
            layer.setRenderer(QgsSingleSymbolRenderer(factory()))
            layer.triggerRepaint()


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
        self.setWindowTitle('Sélectionner une commune')
        self.setMinimumWidth(400)
        self.selected_commune = None
        self._features = features

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f'{len(features)} communes correspondent à votre recherche.\n'
            'Sélectionnez la commune souhaitée :'
        ))

        self._list = QListWidget()
        for feat in features:
            p    = feat['properties']
            nom  = p['nom']
            code = p['code']
            # Afficher le nom et le code INSEE complet pour lever toute ambiguïté
            self._list.addItem(f'{nom}  —  {code}')
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
            p    = feat['properties']
            self.selected_commune = {
                'nom':      p['nom'],
                'code':     p['code'],
                'geometry': feat['geometry'],
            }
        super().accept()
