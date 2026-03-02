# -*- coding: utf-8 -*-
"""
FDP par Commune — Génération automatique d'un fond de plan communal
Script QGIS Processing Toolbox

Installation :
    Traitement > Options > Traitement > Scripts > Dossiers des scripts
    → pointer vers le dossier contenant ce fichier, puis recharger les fournisseurs.
"""

import json
import os
import time
import traceback

import requests
from osgeo import ogr

from qgis.PyQt.QtCore import QVariant
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
    QgsPointXY,
    QgsFeature,
    QgsField,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsSingleSymbolRenderer,
    QgsRuleBasedRenderer,
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
        feedback.setProgress(5)

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
        feedback.setProgress(10)

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
            ('BDTOPO_V3:troncon_de_voie_ferree',
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
            feedback.setProgress(10 + int((i + 1) * progress_per_layer))

        # ── 4. Établissements SIRENE ──────────────────────────────────────────
        if not feedback.isCanceled():
            feedback.pushInfo('Chargement des établissements SIRENE…')
            sirene_layer = self._load_sirene(
                insee, boundary_layer, crs_2154, feedback
            )
            if sirene_layer:
                loaded_layers['sirene'] = sirene_layer
        feedback.setProgress(80)

        # ── 5. Groupe QGIS + symbologie + ajout des couches ──────────────────
        # group.addLayer() place chaque couche en bas de la liste des enfants
        # (dernier index). Dans la légende QGIS, l'index 0 est EN HAUT et est
        # rendu EN DERNIER (par-dessus tout). Donc : la première couche ajoutée
        # ici se retrouve au SOMMET de la légende (rendue par-dessus).
        # Ordre : couche la plus haute en premier, couche de fond en dernier.
        layer_order = [
            'sirene',           # index 0 → sommet → rendu par-dessus
            'buildings',
            'roads',
            'railways',
            'vegetation',
            'rivers',
            'water_surface',
            'parcels',
            'commune_boundary', # dernier → fond → rendu en dessous de tout
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
        feedback.setProgress(90)

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

        feedback.setProgress(100)
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
        insee: str,
        boundary_layer: QgsVectorLayer,
        crs_2154: QgsCoordinateReferenceSystem,
        feedback,
    ):
        """
        Récupère les établissements actifs via l'API Recherche d'entreprises
        (recherche-entreprises.api.gouv.fr), page par page, et construit une
        couche mémoire point directement en mémoire — aucun fichier temporaire.

        L'API est limitée à 10 000 résultats (contrainte Elasticsearch) ;
        un avertissement est émis si ce seuil est atteint.
        """
        API_URL  = 'https://recherche-entreprises.api.gouv.fr/search'
        PER_PAGE = 25   # maximum autorisé par l'API

        # ── Couche mémoire point (EPSG:4326) avec les champs utiles ──────────
        mem_layer = QgsVectorLayer('Point?crs=EPSG:4326', 'SIRENE_raw', 'memory')
        pr = mem_layer.dataProvider()
        pr.addAttributes([
            QgsField('siret',                           QVariant.String),
            QgsField('nom',                             QVariant.String),
            # Nom de champ identique à l'ancien CSV : le renderer NAF ne change pas
            QgsField('activitePrincipaleEtablissement', QVariant.String),
            QgsField('adresse',                         QVariant.String),
        ])
        mem_layer.updateFields()

        page        = 1
        total_pages = 1   # mis à jour après la première réponse
        count       = 0

        try:
            while page <= total_pages:
                if feedback.isCanceled():
                    return None

                resp = requests.get(
                    API_URL,
                    params={'code_commune': insee, 'per_page': PER_PAGE, 'page': page},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()

                # ── Pagination (lue une seule fois depuis la 1re réponse) ─────
                if page == 1:
                    total_results = data.get('total_results', 0)
                    total_pages   = data.get('total_pages', 1)
                    feedback.pushInfo(
                        f'  API SIRENE : {total_results} résultat(s) '
                        f'sur {total_pages} page(s).'
                    )
                    if total_results >= 10000:
                        feedback.pushWarning(
                            '  ⚠ Plus de 10 000 établissements — la couche SIRENE '
                            'sera incomplète (limite Elasticsearch de l\'API).'
                        )

                # ── Extraction des établissements ─────────────────────────────
                features = []
                for company in data.get('results', []):
                    nom_complet = company.get('nom_complet', '')
                    for etab in company.get('matching_etablissements', []):
                        # Garder uniquement les établissements actifs
                        if etab.get('etat_administratif') != 'A':
                            continue
                        lat = etab.get('latitude')
                        lon = etab.get('longitude')
                        if not lat or not lon:
                            continue
                        try:
                            geom = QgsGeometry.fromPointXY(
                                QgsPointXY(float(lon), float(lat))
                            )
                        except (ValueError, TypeError):
                            continue
                        feat = QgsFeature(mem_layer.fields())
                        feat.setGeometry(geom)
                        feat.setAttribute('siret',   etab.get('siret', ''))
                        feat.setAttribute('nom',     nom_complet)
                        feat.setAttribute(
                            'activitePrincipaleEtablissement',
                            etab.get('activite_principale', ''),
                        )
                        feat.setAttribute('adresse', etab.get('adresse', ''))
                        features.append(feat)
                        count += 1

                pr.addFeatures(features)
                feedback.pushInfo(
                    f'  Page {page}/{total_pages} — {count} établissement(s) collecté(s)…'
                )
                page += 1

                # Respecter la limite ~7 req/sec de l'API
                if page <= total_pages:
                    time.sleep(0.15)

            mem_layer.updateExtents()
            feedback.pushInfo(f'  {count} établissement(s) actif(s) chargé(s).')

            if count == 0:
                return None

            # ── Reprojection EPSG:4326 → EPSG:2154 ───────────────────────────
            reprojected = processing.run('native:reprojectlayer', {
                'INPUT':      mem_layer,
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
            feedback.pushWarning(f'  ⚠ Erreur HTTP API SIRENE : {e}')
            return None
        except Exception as e:
            feedback.pushWarning(
                f'  ⚠ Erreur API SIRENE : {e}\n{traceback.format_exc()}'
            )
            return None

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
            # Parcelles cadastrales : remplissage gris très clair, sans contour
            'parcels': lambda: QgsFillSymbol.createSimple({
                'color':         '#e0e0e0',
                'outline_style': 'no',
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
        }

        if style_key == 'sirene':
            self._apply_sirene_style(layer)
            return

        factory = style_factories.get(style_key)
        if factory:
            layer.setRenderer(QgsSingleSymbolRenderer(factory()))
            layer.triggerRepaint()

    def _apply_sirene_style(self, layer: QgsVectorLayer):
        """
        Rendu règle par règle des établissements SIRENE, colorés par section
        d'activité NAF (premier caractère du code APE).
        Les libellés sont en français pour que la légende soit lisible.
        """
        # Format : (code_section, libellé, couleur, forme_marqueur)
        #
        # forme = 'circle'  → présence probable en rez-de-chaussée (flux piéton) :
        #                      commerce, restauration, santé, enseignement,
        #                      culture/sport, services personnels
        # forme = 'square'  → bureau ou activité sans front de rue :
        #                      industrie, transport, finance, services tertiaires
        #
        # Classification basée sur la section NAF. Les cas limites (banques,
        # agences immobilières, cabinets médicaux isolés) peuvent être ajustés
        # en changeant 'square' en 'circle' sur les lignes K, L ou M.
        naf_sections = [
            ('A', 'Agriculture, sylviculture et pêche',              '#74C69D', 'square'),
            ('B', 'Industries extractives',                          '#6C757D', 'square'),
            ('C', 'Industrie manufacturière',                        '#4A4E69', 'square'),
            ('D', 'Énergie — production et distribution',            '#2D6A4F', 'square'),
            ('E', 'Eau, assainissement, déchets',                    '#52B788', 'square'),
            ('F', 'Construction',                                    '#A67C52', 'square'),
            ('G', 'Commerce ; réparation automobile',                '#F4A261', 'circle'),
            ('H', 'Transports et entreposage',                       '#E76F51', 'square'),
            ('I', 'Hébergement et restauration',                     '#E63946', 'circle'),
            ('J', 'Information et communication',                    '#38B2AC', 'square'),
            ('K', 'Activités financières et d\'assurance',           '#1F4E79', 'square'),
            ('L', 'Activités immobilières',                          '#457B9D', 'square'),
            ('M', 'Services spécialisés, scientifiques, techniques', '#9B5DE5', 'square'),
            ('N', 'Services administratifs et de soutien',           '#C77DFF', 'square'),
            ('O', 'Administration publique',                         '#9B2226', 'square'),
            ('P', 'Enseignement',                                    '#FFD166', 'circle'),
            ('Q', 'Santé humaine et action sociale',                 '#06D6A0', 'circle'),
            ('R', 'Arts, spectacles et activités récréatives',       '#118AB2', 'circle'),
            ('S', 'Autres activités de services',                    '#F48FB1', 'circle'),
            ('T', 'Ménages employeurs',                              '#B0BEC5', 'square'),
            ('U', 'Activités extraterritoriales',                    '#E0E0E0', 'square'),
        ]

        root_rule = QgsRuleBasedRenderer.Rule(None)

        for code, label, color, shape in naf_sections:
            sym = QgsMarkerSymbol.createSimple({
                'color':         color,
                'name':          shape,           # 'circle' ou 'square'
                'size':          '2.5' if shape == 'circle' else '1.8',
                'outline_style': 'no',
            })
            rule = QgsRuleBasedRenderer.Rule(sym)
            # left() extrait la section depuis le code APE (ex. "47.11Z" → "4",
            # mais la section est le premier CARACTÈRE ALPHABÉTIQUE du code,
            # qui en réalité précède le code numérique dans la nomenclature.
            # Dans le fichier SIRENE, activitePrincipaleEtablissement contient
            # le code APE numérique (ex. "47.11Z") ; on identifie la section
            # via la table de correspondance division→section ci-dessous.
            # Pour simplifier l'expression QGIS, on utilise une règle sur
            # les deux premiers chiffres du code plutôt que la lettre de section,
            # mais la lettre de section est plus robuste — voir commentaire
            # dans _naf_section_expr().
            rule.setFilterExpression(self._naf_section_expr(code))
            rule.setLabel(label)
            root_rule.appendChild(rule)

        # Règle de repli pour les codes non reconnus
        other_sym = QgsMarkerSymbol.createSimple({
            'color':         '#999999',
            'size':          '1.5',
            'outline_style': 'no',
        })
        other_rule = QgsRuleBasedRenderer.Rule(other_sym)
        other_rule.setFilterExpression('ELSE')
        other_rule.setLabel('Activité non classée')
        root_rule.appendChild(other_rule)

        layer.setRenderer(QgsRuleBasedRenderer(root_rule))
        layer.triggerRepaint()

    @staticmethod
    def _naf_section_expr(section_letter: str) -> str:
        """
        Renvoie une expression QGIS qui correspond à un code APE appartenant
        à la section NAF donnée.

        Le code APE dans SIRENE est au format "DD.DDL" (ex. "47.11Z").
        La correspondance division→section est définie par l'INSEE :
        on teste si le numéro de division (2 premiers chiffres) tombe dans
        la plage de chaque section.

        Plages de divisions par section (nomenclature NAF rév. 2) :
          A:01-03  B:05-09  C:10-33  D:35     E:36-39  F:41-43
          G:45-47  H:49-53  I:55-56  J:58-63  K:64-66  L:68
          M:69-75  N:77-82  O:84     P:85     Q:86-88  R:90-93
          S:94-96  T:97-98  U:99
        """
        # Plages de numéros de division (entiers) pour chaque section
        ranges = {
            'A': [(1,  3)],
            'B': [(5,  9)],
            'C': [(10, 33)],
            'D': [(35, 35)],
            'E': [(36, 39)],
            'F': [(41, 43)],
            'G': [(45, 47)],
            'H': [(49, 53)],
            'I': [(55, 56)],
            'J': [(58, 63)],
            'K': [(64, 66)],
            'L': [(68, 68)],
            'M': [(69, 75)],
            'N': [(77, 82)],
            'O': [(84, 84)],
            'P': [(85, 85)],
            'Q': [(86, 88)],
            'R': [(90, 93)],
            'S': [(94, 96)],
            'T': [(97, 98)],
            'U': [(99, 99)],
        }
        # Construire les clauses BETWEEN sur to_int(left(code, 2))
        # left("activitePrincipaleEtablissement", 2) donne "47" pour "47.11Z"
        field = 'to_int(left("activitePrincipaleEtablissement", 2))'
        clauses = [
            f'({field} BETWEEN {lo} AND {hi})'
            for lo, hi in ranges.get(section_letter, [])
        ]
        return ' OR '.join(clauses) if clauses else 'FALSE'


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
