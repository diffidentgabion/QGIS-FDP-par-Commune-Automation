# -*- coding: utf-8 -*-
"""
fdp_bati_dans_zone.py — Bâti dans une couche de zones polygones quelconque

Apparaît dans la boîte à outils Processing comme :
    "FDP — Bâti dans zone"

Paramètres :
  ZONE_LAYER   : toute couche polygone (ZAE Grand Lyon, ZAI, PLU, etc.)
  CUSTOM_COLOR : couleur de remplissage optionnelle ;
                 si absente, une teinte plus sombre de la couleur source est calculée.

Pour chaque groupe-commune ayant une couche « Bâti » directe :
  - Identifie les bâtiments intersectant les polygones de la couche source
  - Crée une couche mémoire "Bâti — <nom de la couche source>"
  - Insère un groupe "<nom de la couche source>" dans le groupe-commune
  - Remplace le groupe existant si déjà présent
"""

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsCoordinateTransform,
    QgsFillSymbol,
    QgsGeometry,
    QgsGraduatedSymbolRenderer,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterColor,
    QgsProcessingParameterMapLayer,
    QgsProject,
    QgsRuleBasedRenderer,
    QgsSingleSymbolRenderer,
    QgsSpatialIndex,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor


class FDPBatiDansZone(QgsProcessingAlgorithm):

    ZONE_LAYER   = "ZONE_LAYER"
    CUSTOM_COLOR = "CUSTOM_COLOR"

    def name(self):
        return "fdp_bati_dans_zone"

    def displayName(self):
        return "FDP — Bâti dans zone"

    def group(self):
        return "Fond de Plan"

    def groupId(self):
        return "fond_de_plan"

    def createInstance(self):
        return FDPBatiDansZone()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMapLayer(
                self.ZONE_LAYER,
                "Couche de zones (polygones)",
                types=[QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterColor(
                self.CUSTOM_COLOR,
                "Couleur (optionnel — si vide : teinte sombre dérivée de la couche source)",
                optional=True,
                defaultValue=QColor(),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        zone_layer = self.parameterAsVectorLayer(parameters, self.ZONE_LAYER, context)
        if zone_layer is None or not zone_layer.isValid():
            raise Exception("Couche de zones invalide ou introuvable.")

        custom_color = self.parameterAsColor(parameters, self.CUSTOM_COLOR, context)
        fill_color   = custom_color if custom_color.isValid() else self._derive_color(zone_layer)

        zone_name  = zone_layer.name()
        group_name = zone_name
        layer_name = f"Bâti — {zone_name}"

        feedback.pushInfo(f"🎨  Couleur : {fill_color.name()}")

        root            = QgsProject.instance().layerTreeRoot()
        commune_entries = self._find_communes_with_bati(root)

        if not commune_entries:
            raise Exception(
                "Aucune couche « Bâti » trouvée dans les groupes du projet."
            )

        feedback.pushInfo(f"🔎  {len(commune_entries)} commune(s) avec couche Bâti.")

        ok_count   = 0
        skip_count = 0
        total      = len(commune_entries)

        # Géométries des zones lues UNE seule fois (au lieu d'une relecture + une
        # reprojection à chaque commune). Le résultat reprojeté est mémorisé par
        # CRS cible : comme toutes les communes FDP sont en Lambert-93, la
        # reprojection n'a lieu qu'une fois pour l'ensemble du traitement.
        zone_src_crs   = zone_layer.crs()
        zone_geoms_src = []
        for zone_feat in zone_layer.getFeatures():
            zg = QgsGeometry(zone_feat.geometry())
            if zg and not zg.isEmpty():
                zone_geoms_src.append(zg)
        _zone_cache = {}  # authid CRS cible -> list[QgsGeometry] reprojetées

        def _zones_in_crs(target_crs):
            key = target_crs.authid()
            geoms = _zone_cache.get(key)
            if geoms is None:
                if zone_src_crs.authid() == key:
                    geoms = zone_geoms_src
                else:
                    xf = QgsCoordinateTransform(
                        zone_src_crs, target_crs, QgsProject.instance()
                    )
                    geoms = []
                    for zg in zone_geoms_src:
                        zt = QgsGeometry(zg)
                        zt.transform(xf)
                        geoms.append(zt)
                _zone_cache[key] = geoms
            return geoms

        for i, (group, bati_layer) in enumerate(commune_entries):
            if feedback.isCanceled():
                break

            nom = group.name()
            feedback.pushInfo(f"\n[{i + 1}/{total}]  {nom}")
            feedback.setProgress(int(100 * i / total))

            # Supprimer le groupe existant s'il est présent
            for child in list(group.children()):
                if isinstance(child, QgsLayerTreeGroup) and child.name() == group_name:
                    for layer_node in child.findLayers():
                        QgsProject.instance().removeMapLayer(layer_node.layerId())
                    group.removeChildNode(child)
                    feedback.pushInfo(f"   ♻  Groupe « {group_name} » existant supprimé.")

            # CRS des bâtiments (les zones sont reprojetées vers ce CRS via le
            # cache _zones_in_crs construit avant la boucle)
            bati_crs = bati_layer.crs()

            # Index spatial sur les bâtiments (on garde la feature entière — sa
            # géométrie sert au test d'intersection ET à la couche de sortie)
            bld_index = QgsSpatialIndex()
            bld_feat  = {}
            for feat in bati_layer.getFeatures():
                bld_index.addFeature(feat)
                bld_feat[feat.id()] = feat

            if not bld_feat:
                feedback.pushInfo("   ℹ  Couche Bâti vide — commune ignorée.")
                skip_count += 1
                continue

            # Appariement bâtiments × zones (géométries de zones déjà lues et
            # reprojetées une seule fois par _zones_in_crs)
            matched_fids = set()
            for zone_geom in _zones_in_crs(bati_crs):
                if feedback.isCanceled():
                    break
                for bld_fid in bld_index.intersects(zone_geom.boundingBox()):
                    if zone_geom.intersects(bld_feat[bld_fid].geometry()):
                        matched_fids.add(bld_fid)

            if not matched_fids:
                feedback.pushInfo("   ℹ  Aucun bâtiment dans les zones pour cette commune.")
                skip_count += 1
                continue

            feedback.pushInfo(f"   ✓  {len(matched_fids)} bâtiment(s) dans les zones")

            # Couche mémoire
            fields = bati_layer.fields()
            lyr    = QgsVectorLayer(f"Polygon?crs={bati_crs.authid()}", layer_name, "memory")
            pr     = lyr.dataProvider()
            pr.addAttributes(fields.toList())
            lyr.updateFields()
            pr.addFeatures([bld_feat[fid] for fid in matched_fids])
            lyr.updateExtents()

            sym = QgsFillSymbol.createSimple({"color": fill_color.name(), "outline_style": "no"})
            lyr.setRenderer(QgsSingleSymbolRenderer(sym))

            zone_grp = group.addGroup(group_name)
            QgsProject.instance().addMapLayer(lyr, False)
            zone_grp.addLayer(lyr)
            ok_count += 1

        feedback.setProgress(100)
        feedback.pushInfo(
            f"\n✅  {ok_count} commune(s) traitée(s)"
            + (f", {skip_count} ignorée(s)." if skip_count else ".")
        )
        return {}

    # =========================================================================
    # Helpers
    # =========================================================================

    def _derive_color(self, layer):
        """Extrait la couleur de remplissage de la couche source et la rend plus sombre."""
        return self._darken(self._extract_fill_color(layer))

    @staticmethod
    def _extract_fill_color(layer):
        """Retourne la QColor du premier symbole du renderer, ou gris si introuvable."""
        renderer = layer.renderer()
        if renderer is None:
            return QColor("#888888")

        symbol = None
        if isinstance(renderer, QgsSingleSymbolRenderer):
            symbol = renderer.symbol()
        elif isinstance(renderer, QgsCategorizedSymbolRenderer):
            cats = renderer.categories()
            if cats:
                symbol = cats[0].symbol()
        elif isinstance(renderer, QgsGraduatedSymbolRenderer):
            ranges = renderer.ranges()
            if ranges:
                symbol = ranges[0].symbol()
        elif isinstance(renderer, QgsRuleBasedRenderer):
            rules = renderer.rootRule().children()
            if rules:
                symbol = rules[0].symbol()

        if symbol is None:
            return QColor("#888888")
        color = symbol.color()
        return color if color.isValid() else QColor("#888888")

    @staticmethod
    def _darken(color):
        """Retourne une teinte plus sombre (+30 % saturation, −45 % valeur)."""
        return QColor.fromHsvF(
            max(color.hsvHueF(), 0.0),
            min(color.hsvSaturationF() * 1.3, 1.0),
            max(color.valueF() * 0.55, 0.0),
        )

    def _find_communes_with_bati(self, root):
        """Retourne list[(group, bati_layer)] pour chaque groupe ayant une couche « Bâti » directe."""
        results = []
        for child in root.children():
            if not isinstance(child, QgsLayerTreeGroup):
                continue
            bati = self._find_direct_bati(child)
            if bati is not None:
                results.append((child, bati))
            else:
                for grandchild in child.children():
                    if isinstance(grandchild, QgsLayerTreeGroup):
                        bati = self._find_direct_bati(grandchild)
                        if bati is not None:
                            results.append((grandchild, bati))
        return results

    @staticmethod
    def _find_direct_bati(group):
        """Retourne la couche enfant directe nommée « Bâti », ou None."""
        for node in group.children():
            if not isinstance(node, QgsLayerTreeLayer):
                continue
            layer = QgsProject.instance().mapLayer(node.layerId())
            if layer and layer.name() == "Bâti":
                return layer
        return None
