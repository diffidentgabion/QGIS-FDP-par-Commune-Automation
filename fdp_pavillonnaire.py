# -*- coding: utf-8 -*-
"""
fdp_pavillonnaire.py — Groupe Pavillonnaire pour toutes les communes FDP

Apparaît dans la boîte à outils Processing comme :
    "FDP — Créer groupe Pavillonnaire"

Pour chaque groupe-commune du projet (fdp_insee présent) :
  - Cherche "Bâti — Résidentiel" dans le sous-groupe "Bâti intrinsèque"
  - Filtre les bâtiments à ≤ 2 étages (nombre_d_etages ≤ 2, 0/NULL inclus)
  - Crée une couche mémoire "Bâti — Pavillonnaire"
  - Insère un groupe "Pavillonnaire" dans le groupe-commune (ignoré si déjà présent)
"""

from qgis.core import (
    QgsFillSymbol,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsProcessingAlgorithm,
    QgsProject,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)


class FDPPavillonnaire(QgsProcessingAlgorithm):

    def name(self):
        return "fdp_pavillonnaire"

    def displayName(self):
        return "FDP — Créer groupe Pavillonnaire"

    def group(self):
        return "Fond de Plan"

    def groupId(self):
        return "fond_de_plan"

    def createInstance(self):
        return FDPPavillonnaire()

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        root = QgsProject.instance().layerTreeRoot()
        commune_groups = self._find_commune_groups(root)

        if not commune_groups:
            raise Exception(
                "Aucun groupe-commune trouvé. "
                "Les groupes doivent avoir la propriété fdp_insee "
                "(importés via « FDP par Commune »)."
            )

        feedback.pushInfo(f"🔎  {len(commune_groups)} commune(s) détectée(s).")

        ok_count   = 0
        skip_count = 0
        total      = len(commune_groups)

        for i, group in enumerate(commune_groups):
            if feedback.isCanceled():
                break

            nom = group.name()
            feedback.pushInfo(f"\n[{i + 1}/{total}]  {nom}")
            feedback.setProgress(int(100 * i / total))

            # Supprimer le groupe "Pavillonnaire" existant s'il est présent
            for child in list(group.children()):
                if isinstance(child, QgsLayerTreeGroup) and child.name() == "Pavillonnaire":
                    for layer_node in child.findLayers():
                        QgsProject.instance().removeMapLayer(layer_node.layerId())
                    group.removeChildNode(child)
                    feedback.pushInfo("   ♻  Groupe Pavillonnaire existant supprimé.")

            # Trouver "Bâti — Résidentiel" dans "Bâti intrinsèque"
            residentiel_layer = self._find_residentiel_layer(group)
            if residentiel_layer is None:
                feedback.pushInfo(
                    "   ⚠  Couche « Bâti — Résidentiel » introuvable dans "
                    "« Bâti intrinsèque » — commune ignorée."
                )
                skip_count += 1
                continue

            # Construire la couche pavillonnaire (résidentiel ≤ 2 étages)
            pav_layer = self._make_pavillonnaire_layer(residentiel_layer)

            if pav_layer.featureCount() == 0:
                feedback.pushInfo("   ℹ  Aucun bâtiment pavillonnaire (résidentiel ≤ 2 étages).")
                skip_count += 1
                continue

            feedback.pushInfo(
                f"   ✓  {pav_layer.featureCount()} bâtiment(s) pavillonnaire(s)"
            )

            pav_grp = group.addGroup("Pavillonnaire")
            QgsProject.instance().addMapLayer(pav_layer, False)
            pav_grp.addLayer(pav_layer)
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

    def _find_commune_groups(self, root):
        """
        Retourne les groupes ayant un sous-groupe "Bâti intrinsèque".
        Cherche en profondeur 1 (enfants de root) et profondeur 2
        (petits-enfants, pour les communes dans un groupe organisationnel).
        """
        groups = []
        for child in root.children():
            if not isinstance(child, QgsLayerTreeGroup):
                continue
            if self._has_bati_intrinsèque(child):
                groups.append(child)
            else:
                for grandchild in child.children():
                    if isinstance(grandchild, QgsLayerTreeGroup):
                        if self._has_bati_intrinsèque(grandchild):
                            groups.append(grandchild)
        return groups

    def _has_bati_intrinsèque(self, group):
        return any(
            isinstance(c, QgsLayerTreeGroup) and c.name() == "Bâti intrinsèque"
            for c in group.children()
        )

    def _find_residentiel_layer(self, commune_group):
        """
        Cherche la couche "Bâti — Résidentiel" dans le sous-groupe
        "Bâti intrinsèque" du groupe-commune.
        """
        for child in commune_group.children():
            if not isinstance(child, QgsLayerTreeGroup):
                continue
            if child.name() == "Bâti intrinsèque":
                for node in child.children():
                    if not isinstance(node, QgsLayerTreeLayer):
                        continue
                    layer = QgsProject.instance().mapLayer(node.layerId())
                    if layer and layer.name() == "Bâti — Résidentiel":
                        return layer
        return None

    def _make_pavillonnaire_layer(self, residentiel_layer):
        """
        Crée une couche mémoire avec les bâtiments résidentiels à ≤ 2 étages.

        nombre_d_etages == 0 ou NULL : non renseigné dans BDTOPO → inclus,
        car la plupart des bâtiments pavillonnaires plain-pied ne sont pas
        documentés dans la base.
        """
        crs_id = residentiel_layer.crs().authid()
        fields = residentiel_layer.fields()

        lyr = QgsVectorLayer(f"Polygon?crs={crs_id}", "Bâti — Pavillonnaire", "memory")
        pr  = lyr.dataProvider()
        pr.addAttributes(fields.toList())
        lyr.updateFields()

        feats = [f for f in residentiel_layer.getFeatures() if self._is_pavillonnaire(f)]
        pr.addFeatures(feats)
        lyr.updateExtents()

        sym = QgsFillSymbol.createSimple({"color": "#C8956C", "outline_style": "no"})
        lyr.setRenderer(QgsSingleSymbolRenderer(sym))
        return lyr

    @staticmethod
    def _is_pavillonnaire(feat) -> bool:
        """
        True si nombre_d_etages ≤ 2 ET usage_1 est vide ou "résidentiel".
        Tout autre usage_1 (industriel, commercial…) exclut le bâtiment.
        """
        v = feat["nombre_d_etages"]
        try:
            if int(v) > 2:
                return False
        except (TypeError, ValueError):
            pass  # NULL/0 → plain-pied probable, on continue

        u = feat["usage_1"]
        usage = "" if (u is None or str(u).strip() == "NULL") else str(u).strip().lower()
        return usage in ("", "résidentiel")
