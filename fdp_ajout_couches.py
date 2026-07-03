# -*- coding: utf-8 -*-
"""
fdp_ajout_couches.py — Ajout de couches aux communes déjà importées (batch)

Apparaît dans la boîte à outils Processing comme :
    "FDP — Ajout aux communes existantes"

Pour chaque groupe à la racine du projet (= commune déjà importée) :
  - ré-interroge l'API Géo pour obtenir l'emprise et le code INSEE
  - charge les couches sélectionnées qui ne sont pas encore présentes
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
    QgsProject,
    QgsVectorLayer,
)
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

    def createInstance(self):
        return FDPAjoutCouches()

    def initAlgorithm(self, config=None):
        pass  # aucun paramètre — les communes sont lues depuis le projet

    # =========================================================================

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
        if not selected_entries:
            raise Exception("Aucune couche sélectionnée.")

        sirene_entry = next((e for e in selected_entries if e["style_key"] == "sirene"), None)
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

        feedback.pushInfo(f"🗺  {len(commune_nodes)} commune(s) identifiée(s).")

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
                if entry["display_name"] in existing_names:
                    feedback.pushInfo(f"   ↷  {entry['display_name']} déjà présent.")
                    continue
                try:
                    layer = self._load_wfs_layer(
                        entry["typename"], entry["display_name"],
                        bbox, boundary_layer, crs_2154, feedback,
                    )
                    if layer:
                        self._apply_style(layer, entry["style_key"])
                        QgsProject.instance().addMapLayer(layer, False)
                        group.addLayer(layer)
                        existing_names.add(layer.name())
                        any_added = True
                except Exception as e:
                    feedback.reportError(
                        f"   ⚠  {entry['display_name']} : {e}", fatalError=False
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

    # _lookup_commune_batch is inherited from FDPParCommune
