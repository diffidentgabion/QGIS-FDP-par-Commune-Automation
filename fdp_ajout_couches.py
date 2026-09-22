# -*- coding: utf-8 -*-
"""
fdp_ajout_couches.py — Ajout de couches aux communes déjà importées (batch)

Apparaît dans la boîte à outils Processing comme :
    "FDP — Ajout aux communes existantes"

Pour chaque groupe à la racine du projet (= commune déjà importée) :
  - ré-interroge l'API Géo pour obtenir l'emprise et le code INSEE
  - charge les couches sélectionnées qui ne sont pas encore présentes
  - charge la topographie (WFS ou LiDAR HD) si demandée et absente —
    sélectionnable seule, sans aucune autre couche
  - pour SIRENE, utilise la première couche polygone du groupe comme bâtiments
"""

import importlib.util
import os

import requests
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsProcessingParameterString,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QPainter
from qgis.PyQt.QtWidgets import QDialog

# ── Chargement des modules voisins (pas de __package__ en script Processing) ──
def _load_sibling(module_name):
    """Charge et renvoie le module .py voisin `module_name` (sans extension)."""
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), module_name + ".py"
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_sibling("fdp_par_commune")
FDPParCommune = _mod.FDPParCommune
_LayerSelectorDialog = _mod._LayerSelectorDialog
_LEGACY_DISPLAY_NAMES = _mod._LEGACY_DISPLAY_NAMES

build_displaced_sirene_layer = _load_sibling("sirene_display").build_displaced_sirene_layer
build_activity_layers = _load_sibling("sirene_buildings").build_activity_layers
del _mod

# Codes INSEE des communes-mères PLM (établissements stockés sous arrondissements)
_PLM_PARENT_CODES = {"75056", "69123", "13055"}


class FDPAjoutCouches(FDPParCommune):
    """
    Hérite de FDPParCommune pour réutiliser tous les helpers de chargement
    et de style. Surcharge uniquement l'identité et processAlgorithm.
    """

    def name(self):
        return "fdp_ajout_couches"

    def displayName(self):
        return "FDP — Ajout aux communes existantes"

    FILTRE_COMMUNES = "FILTRE_COMMUNES"

    def shortHelpString(self):
        return (
            "Ajoute des couches aux communes déjà importées dans le projet.\n\n"
            "Chaque groupe commune reçoit les couches sélectionnées qui n'y "
            "sont pas encore ; celles déjà présentes sont ignorées.\n\n"
            "Laissez le champ vide pour traiter toutes les communes, ou "
            "indiquez-en quelques-unes (une par ligne — nom ou code INSEE)."
        )

    def createInstance(self):
        return FDPAjoutCouches()

    def initAlgorithm(self, config=None):
        # Ce paramètre est aussi ce qui fait ouvrir à QGIS la fenêtre
        # Processing standard : sans paramètre visible, QGIS lance l'outil
        # directement avec un MessageBarProgress, dont close() — appelé par
        # chaque processing.run imbriqué — affiche un dialogue modal
        # « Problem executing algorithm » à chaque géométrie invalide ignorée.
        self.addParameter(
            QgsProcessingParameterString(
                self.FILTRE_COMMUNES,
                "Limiter aux communes (une par ligne — nom ou code INSEE ; vide = toutes)",
                multiLine=True,
                optional=True,
            )
        )

    # =========================================================================

    @staticmethod
    def _divisions_group(group):
        """
        Sous-groupe « Divisions communales » du groupe commune, créé au besoin.
        Même nom qu'à l'import initial (voir _DIVISIONS_KEYS dans
        fdp_par_commune) pour qu'un ajout vienne compléter le groupe existant
        au lieu d'en créer un second.
        """
        existing = group.findGroup("Divisions communales")
        return existing if existing is not None else group.addGroup("Divisions communales")

    def processAlgorithm(self, parameters, context, feedback):
        root = QgsProject.instance().layerTreeRoot()
        all_groups = self._collect_commune_candidates(root)

        if not all_groups:
            raise Exception(
                "Aucun groupe dans le projet. "
                "Importez d'abord des communes avec « FDP par Commune »."
            )

        feedback.pushInfo(f"🔎  {len(all_groups)} groupe(s) trouvé(s), résolution des communes…")

        # ── Sélection des couches ─────────────────────────────────────────────
        dlg = _LayerSelectorDialog()
        if dlg.exec_() != QDialog.Accepted:
            raise Exception("Annulé.")
        selected_entries = dlg.result_layers
        topo_config = dlg.topo_config
        if not selected_entries and not topo_config:
            raise Exception("Aucune couche ni topographie sélectionnée.")

        sirene_entry = next((e for e in selected_entries if e["style_key"] == "sirene"), None)
        # Arbres (OSM) : comme SIRENE, cette couche n'a pas de typename WFS
        # (source Overpass) — elle est donc absente de wfs_entries et doit être
        # traitée par son propre bloc plus bas.
        arbres_entry = next((e for e in selected_entries if e["style_key"] == "osm_arbres"), None)
        # Quartiers prioritaires : source GPKG data.gouv, donc également
        # absente de wfs_entries et traitée par son propre bloc.
        qpv_entry    = next((e for e in selected_entries if e["style_key"] == "qpv"), None)
        wfs_entries  = [e for e in selected_entries if e["typename"] is not None]

        crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
        crs_2154 = QgsCoordinateReferenceSystem("EPSG:2154")
        xform    = QgsCoordinateTransform(crs_4326, crs_2154, QgsProject.instance())

        # ── Résolution silencieuse : identifier les groupes communes ─────────
        # Les groupes non-communes (Lot X, Bâti intrinsèque…) retournent None
        # sans message — comportement attendu, pas une erreur.
        commune_nodes = []
        for group in all_groups:
            insee_hint = group.customProperty("fdp_insee") or None
            commune    = self._lookup_commune_batch(
                group.name(), insee_hint, feedback,
                warn_on_miss=bool(insee_hint),   # avertir seulement si fdp_insee connu
            )
            if commune is not None:
                commune_nodes.append((group, commune))

        if not commune_nodes:
            raise Exception("Aucune commune reconnue dans les groupes du projet.")

        filtre = [
            t.strip().casefold()
            for t in (self.parameterAsString(parameters, self.FILTRE_COMMUNES, context) or "").splitlines()
            if t.strip()
        ]
        if filtre:
            commune_nodes, non_trouves = self._filter_communes(commune_nodes, filtre)
            for t in non_trouves:
                feedback.pushWarning(f"⚠  « {t} » : aucune commune correspondante dans le projet.")
            if not commune_nodes:
                raise Exception("Aucune commune du projet ne correspond au filtre.")

        feedback.pushInfo(f"🗺  {len(commune_nodes)} commune(s) identifiée(s).")

        # Arbres (OSM) : une seule requête Overpass pour tout le lot au lieu
        # d'une par commune — les instances publiques n'accordent que deux
        # créneaux par IP et refusent le reste (voir _prefetch_osm_trees).
        if arbres_entry and len(commune_nodes) > 1:
            bboxes = []
            for _grp, c in commune_nodes:
                try:
                    bboxes.append(self._commune_bbox(c, xform))
                except Exception:
                    pass  # géométrie illisible : cette commune sera traitée seule
            self._prefetch_osm_trees(bboxes, feedback)

        ok_count   = 0
        skip_count = 0
        total      = len(commune_nodes)

        for i, (group, commune) in enumerate(commune_nodes):
            if feedback.isCanceled():
                break

            nom   = commune["nom"]
            insee = commune["code"]
            feedback.pushInfo(f"\n[{i + 1}/{total}]  {nom}")
            feedback.setProgress(int(100 * i / total))

            try:
                commune_geom = self._geojson_to_qgsgeometry(commune["geometry"])
                commune_geom.transform(xform)
                bbox           = commune_geom.boundingBox()
                boundary_layer = self._geom_to_temp_layer(commune_geom, "Polygon", crs_2154)
            except Exception as e:
                feedback.reportError(f"   ⚠  Géométrie : {e} — ignorée.", fatalError=False)
                skip_count += 1
                continue

            # ── Noms des couches déjà présentes dans le groupe (récursif) ─────
            existing_names = {
                QgsProject.instance().mapLayer(ll.layerId()).name()
                for ll in group.findLayers()
                if QgsProject.instance().mapLayer(ll.layerId())
            }

            any_added = False

            # ── Couches WFS ───────────────────────────────────────────────────
            for entry in wfs_entries:
                if feedback.isCanceled():
                    break
                # Reconnaître aussi les anciens libellés (« Végétation »,
                # « Forêt (BD Forêt V2) »…) pour ne pas recharger en doublon.
                known_names = {entry["display_name"]} | _LEGACY_DISPLAY_NAMES.get(
                    entry["display_name"], frozenset()
                )
                if known_names & existing_names:
                    feedback.pushInfo(f"   ↷  {entry['display_name']} déjà présent.")
                    continue
                try:
                    # insee : déclenche l'année de construction du bâti (BDNB)
                    # dans le chargeur partagé — voir _post_load_extras.
                    layer = self._load_wfs_layer(
                        entry["typename"], entry["display_name"],
                        bbox, boundary_layer, crs_2154, feedback, insee,
                    )
                    if layer:
                        self._apply_style(layer, entry["style_key"])
                        QgsProject.instance().addMapLayer(layer, False)
                        # Les divisions infra-communales vont dans leur propre
                        # sous-groupe, comme à l'import initial.
                        if entry["style_key"] == "iris":
                            self._divisions_group(group).addLayer(layer)
                        else:
                            group.addLayer(layer)
                        existing_names.add(layer.name())
                        any_added = True
                except Exception as e:
                    feedback.reportError(
                        f"   ⚠  {entry['display_name']} : {e}", fatalError=False
                    )

            # ── Arbres (OSM) ──────────────────────────────────────────────────
            if arbres_entry and not feedback.isCanceled():
                if arbres_entry["display_name"] in existing_names:
                    feedback.pushInfo("   ↷  Arbres (OSM) déjà présents.")
                else:
                    try:
                        arbres_layer = self._load_osm_trees(
                            bbox, boundary_layer, crs_2154, feedback
                        )
                        if arbres_layer:
                            self._apply_style(arbres_layer, "osm_arbres")
                            QgsProject.instance().addMapLayer(arbres_layer, False)
                            group.addLayer(arbres_layer)
                            existing_names.add(arbres_layer.name())
                            any_added = True
                    except Exception as e:
                        feedback.reportError(f"   ⚠  Arbres (OSM) : {e}", fatalError=False)

            # ── Quartiers prioritaires (QPV) ──────────────────────────────────
            if qpv_entry and not feedback.isCanceled():
                if qpv_entry["display_name"] in existing_names:
                    feedback.pushInfo("   ↷  Quartiers prioritaires déjà présents.")
                else:
                    try:
                        qpv_layer = self._load_qpv_layer(insee, crs_2154, feedback)
                        if qpv_layer:
                            self._apply_style(qpv_layer, "qpv")
                            QgsProject.instance().addMapLayer(qpv_layer, False)
                            group.addLayer(qpv_layer)
                            existing_names.add(qpv_layer.name())
                            any_added = True
                    except Exception as e:
                        feedback.reportError(
                            f"   ⚠  Quartiers prioritaires : {e}", fatalError=False
                        )

            # ── SIRENE ────────────────────────────────────────────────────────
            if sirene_entry and not feedback.isCanceled():
                if any(("SIRENE" in n or "Établissements" in n) for n in existing_names):
                    feedback.pushInfo("   ↷  SIRENE déjà présent.")
                elif insee in _PLM_PARENT_CODES:
                    feedback.pushInfo(
                        "   ⚠  Ville entière PLM — utilisez « FDP par Commune » "
                        "en cherchant directement l'arrondissement."
                    )
                else:
                    try:
                        sirene_layer = self._load_sirene(
                            insee, boundary_layer, crs_2154, feedback
                        )
                        if sirene_layer:
                            # Chercher la couche bâtiments par nom — pas n'importe
                            # quelle couche polygone (ZAI, emprise… causerait un
                            # regroupement de tous les points au centroïde unique).
                            bati_layer = next(
                                (
                                    QgsProject.instance().mapLayer(ll.layerId())
                                    for ll in group.findLayers()
                                    if QgsProject.instance().mapLayer(ll.layerId())
                                    and QgsProject.instance().mapLayer(ll.layerId()).name() == "Bâti"
                                ),
                                None,
                            )
                            if bati_layer:
                                feedback.pushInfo("   📌  Placement des établissements…")
                                sirene_layer = build_displaced_sirene_layer(
                                    sirene_layer, bati_layer, feedback
                                )
                                # Bâti par activité SIRENE
                                activity_layers = build_activity_layers(
                                    bati_layer, sirene_layer, feedback
                                )
                                if activity_layers:
                                    act_grp = group.addGroup("Bâti par activité SIRENE")
                                    for act_layer in activity_layers:
                                        QgsProject.instance().addMapLayer(act_layer, False)
                                        act_grp.addLayer(act_layer)
                            else:
                                feedback.pushInfo(
                                    "   ℹ  Couche « Bâti » introuvable — "
                                    "points SIRENE placés sans déplacement bâtiment."
                                )
                            self._apply_style(sirene_layer, "sirene")
                            QgsProject.instance().addMapLayer(sirene_layer, False)
                            group.insertChildNode(0, QgsLayerTreeLayer(sirene_layer))
                            any_added = True
                    except Exception as e:
                        feedback.reportError(f"   ⚠  SIRENE : {e}", fatalError=False)

            # ── Topographie ───────────────────────────────────────────────────
            if topo_config and not feedback.isCanceled():
                has_topo = (
                    any(g.name() == "Topographie" for g in group.findGroups())
                    or any(n.startswith("Courbes de niveau") for n in existing_names)
                    or "Ombrage du relief" in existing_names
                )
                if has_topo:
                    feedback.pushInfo("   ↷  Topographie déjà présente.")
                else:
                    try:
                        feedback.pushInfo("   ⛰  Topographie…")
                        topo_layer = None
                        hillshade_layer = None
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
                            feedback.pushInfo(
                                f"   ✓  {topo_layer.featureCount()} courbe(s)"
                            )
                        if topo_layer or hillshade_layer:
                            # Sommet de la légende, comme dans l'import initial.
                            topo_grp = group.insertGroup(0, "Topographie")
                            if topo_layer:
                                QgsProject.instance().addMapLayer(topo_layer, False)
                                topo_grp.addLayer(topo_layer)
                            if hillshade_layer:
                                hillshade_layer.setBlendMode(
                                    QPainter.CompositionMode_Overlay
                                )
                                hillshade_layer.brightnessFilter().setBrightness(-50)
                                QgsProject.instance().addMapLayer(hillshade_layer, False)
                                topo_grp.addLayer(hillshade_layer)
                            any_added = True
                    except Exception as e:
                        feedback.reportError(
                            f"   ⚠  Topographie : {e}", fatalError=False
                        )

            if any_added:
                ok_count += 1

        feedback.setProgress(100)
        feedback.pushInfo(
            f"\n✅  {ok_count} commune(s) mise(s) à jour"
            + (f", {skip_count} ignorée(s)." if skip_count else ".")
        )
        return {}

    # =========================================================================
    # Helper — lookup silencieux (pas de dialogue)
    # =========================================================================

    def _collect_commune_candidates(self, root):
        """
        Retourne les groupes candidats communes sans descendre dans les
        sous-groupes de couches (Bâti intrinsèque, Agriculture…).

        Stratégie :
          - Tout groupe ayant la propriété fdp_insee → commune certaine.
          - Sinon : on cherche à profondeur 1 (enfants directs de root)
            et profondeur 2 (petits-enfants) seulement — les sous-groupes
            de couches sont à profondeur ≥ 3 et ne sont jamais des communes.
        """
        candidates = []
        for child in root.children():
            if not isinstance(child, QgsLayerTreeGroup):
                continue
            if child.customProperty("fdp_insee"):
                candidates.append(child)
            else:
                # Profondeur 1 : peut être une commune directe ou un groupe
                # organisationnel (Lot X…). On l'inclut pour vérification API.
                candidates.append(child)
                # Profondeur 2 : enfants directs du groupe de niveau 1.
                # Si le niveau 1 est organisationnel, ses enfants sont les
                # vraies communes.
                for grandchild in child.children():
                    if isinstance(grandchild, QgsLayerTreeGroup):
                        candidates.append(grandchild)
        return candidates

    @staticmethod
    def _filter_communes(commune_nodes, filtre):
        """
        Garde les (groupe, commune) dont le nom de groupe, le nom officiel ou
        le code INSEE figure dans `filtre` (valeurs déjà en casefold).
        Renvoie (communes retenues, valeurs du filtre sans correspondance).
        """
        kept, matched = [], set()
        for group, commune in commune_nodes:
            keys = {
                group.name().strip().casefold(),
                str(commune.get("nom", "")).casefold(),
                str(commune.get("code", "")).casefold(),
            }
            hits = keys.intersection(filtre)
            if hits:
                kept.append((group, commune))
                matched |= hits
        return kept, [t for t in filtre if t not in matched]

    # _lookup_commune_batch is inherited from FDPParCommune
