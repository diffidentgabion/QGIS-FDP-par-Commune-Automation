# -*- coding: utf-8 -*-
"""
fdp_pavillonnaire.py — Groupe Pavillonnaire pour toutes les communes FDP

Apparaît dans la boîte à outils Processing comme :
    "FDP — Créer groupe Pavillonnaire"

Pour chaque groupe-commune du projet (fdp_insee présent) :
  - Cherche "Bâti — Résidentiel" dans le sous-groupe "Bâti intrinsèque"
  - Filtre les bâtiments pavillonnaires selon les attributs BDTOPO :
      · nombre_d_etages ≤ 2           (0/NULL inclus)
      · nombre_de_logements ≤ 2       (0/NULL inclus)
      · hauteur ≤ MAX_HAUTEUR_M       (NULL inclus)
      · emprise au sol entre MIN_EMPRISE_M2 et MAX_EMPRISE_M2
      · usage_1 et usage_2 vides ou "résidentiel"
      · nature vide ou "indifférenciée"
      · construction_legere faux
  - Crée une couche mémoire "Bâti — Pavillonnaire"
  - Insère un groupe "Pavillonnaire" dans le groupe-commune (ignoré si déjà présent)
"""

from qgis.core import (
    QgsDistanceArea,
    QgsFillSymbol,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsProcessingAlgorithm,
    QgsProject,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)


# =============================================================================
# Seuils pavillonnaires
# =============================================================================

# Hauteur maximale (m). BDTOPO renseigne `hauteur` (photogrammétrie) bien plus
# souvent que `nombre_d_etages` (Fichiers fonciers) : c'est le garde-fou pour
# les bâtiments sans étages renseignés. R+1 avec combles ≈ 9 m.
MAX_HAUTEUR_M = 10.0

# Emprise au sol (m²). En dessous : annexes / garages ayant reçu un logement.
# Au-dessus : manoirs, corps de ferme, longères — pas un pavillon.
MIN_EMPRISE_M2 = 30.0
MAX_EMPRISE_M2 = 400.0

# Valeurs (normalisées lower) acceptées. Le WFS Géoplateforme livre ces
# champs en titlecase ; on applique .strip().lower() avant comparaison.
_USAGES_OK  = {"", "résidentiel"}
_NATURES_OK = {"", "indifférenciée"}

# Libellés des motifs d'exclusion, dans l'ordre d'évaluation.
_MOTIFS = (
    "étages > 2",
    "logements > 2",
    f"hauteur > {MAX_HAUTEUR_M:g} m",
    f"emprise < {MIN_EMPRISE_M2:g} m²",
    f"emprise > {MAX_EMPRISE_M2:g} m²",
    "usage_1 non résidentiel",
    "usage_2 renseigné",
    "nature particulière",
    "construction légère",
)


def _field_str(v) -> str:
    """str normalisée (strip + lower), '' pour None ou PyQGIS NULL."""
    if v is None:
        return ""
    t = str(v).strip()
    return "" if t == "NULL" else t.lower()


def _field_num(v):
    """float ou None pour None / NULL / non numérique."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _attr(feat, name):
    """Valeur d'attribut, ou None si le champ n'existe pas dans la couche."""
    idx = feat.fields().indexOf(name)
    return feat[idx] if idx >= 0 else None


def _field_bool(v) -> bool:
    """Booléen BDTOPO : bool natif ou chaîne 'true'/'false' selon le provider."""
    if isinstance(v, bool):
        return v
    return _field_str(v) in ("true", "t", "1", "oui", "vrai")


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

            # Construire la couche pavillonnaire (filtre attributaire BDTOPO)
            pav_layer, exclusions = self._make_pavillonnaire_layer(residentiel_layer)

            detail = ", ".join(
                f"{n} {motif}" for motif, n in zip(_MOTIFS, exclusions) if n
            )
            if detail:
                feedback.pushInfo(f"   −  exclus : {detail}")

            if pav_layer.featureCount() == 0:
                feedback.pushInfo("   ℹ  Aucun bâtiment pavillonnaire.")
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
        Crée une couche mémoire avec les bâtiments résidentiels pavillonnaires
        (voir _motif_exclusion). Retourne (couche, compteurs d'exclusion),
        les compteurs suivant l'ordre de _MOTIFS.

        nombre_d_etages / nombre_de_logements / hauteur à 0 ou NULL : non
        renseignés dans BDTOPO → inclus, car la plupart des pavillons de
        plain-pied ne sont pas documentés dans la base. L'emprise au sol,
        toujours disponible, sert de garde-fou.
        """
        crs_id = residentiel_layer.crs().authid()
        fields = residentiel_layer.fields()

        lyr = QgsVectorLayer(f"Polygon?crs={crs_id}", "Bâti — Pavillonnaire", "memory")
        pr  = lyr.dataProvider()
        pr.addAttributes(fields.toList())
        lyr.updateFields()

        # Mesure d'emprise en m² quel que soit le CRS de la couche
        # (EPSG:2154 en pratique, mais on ne présume pas).
        da = QgsDistanceArea()
        da.setSourceCrs(residentiel_layer.crs(), QgsProject.instance().transformContext())
        da.setEllipsoid("GRS80")

        feats      = []
        exclusions = [0] * len(_MOTIFS)
        for f in residentiel_layer.getFeatures():
            g = f.geometry()
            area = da.measureArea(g) if (g is not None and not g.isEmpty()) else 0.0
            motif = self._motif_exclusion(f, area)
            if motif is None:
                feats.append(f)
            else:
                exclusions[motif] += 1

        pr.addFeatures(feats)
        lyr.updateExtents()

        sym = QgsFillSymbol.createSimple({"color": "#C8956C", "outline_style": "no"})
        lyr.setRenderer(QgsSingleSymbolRenderer(sym))
        return lyr, exclusions

    @staticmethod
    def _motif_exclusion(feat, area_m2):
        """
        None si le bâtiment est pavillonnaire, sinon l'index dans _MOTIFS
        du premier critère qui l'exclut.

        Critères (tous requis) :
          nombre_d_etages ≤ 2          NULL/0 tolérés
          nombre_de_logements ≤ 2      NULL/0 tolérés (petit collectif exclu)
          hauteur ≤ MAX_HAUTEUR_M      NULL/0 tolérés
          MIN_EMPRISE_M2 ≤ emprise ≤ MAX_EMPRISE_M2
          usage_1 vide ou résidentiel
          usage_2 vide ou résidentiel  (usage mixte = centre-bourg, pas pavillon)
          nature vide ou indifférenciée (château, tour, moulin, serre… exclus)
          construction_legere faux
        """
        v = _field_num(_attr(feat, "nombre_d_etages"))
        if v is not None and v > 2:
            return 0

        v = _field_num(_attr(feat, "nombre_de_logements"))
        if v is not None and v > 2:
            return 1

        v = _field_num(_attr(feat, "hauteur"))
        if v is not None and v > MAX_HAUTEUR_M:
            return 2

        if area_m2 < MIN_EMPRISE_M2:
            return 3
        if area_m2 > MAX_EMPRISE_M2:
            return 4

        if _field_str(_attr(feat, "usage_1")) not in _USAGES_OK:
            return 5
        if _field_str(_attr(feat, "usage_2")) not in _USAGES_OK:
            return 6
        if _field_str(_attr(feat, "nature")) not in _NATURES_OK:
            return 7
        if _field_bool(_attr(feat, "construction_legere")):
            return 8

        return None

    @staticmethod
    def _is_pavillonnaire(feat, area_m2) -> bool:
        """True si aucun critère n'exclut le bâtiment (voir _motif_exclusion)."""
        return FDPPavillonnaire._motif_exclusion(feat, area_m2) is None
