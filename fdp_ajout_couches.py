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

# ── Charger FDPParCommune et ses helpers depuis fdp_par_commune.py ────────────
_fdp_spec = importlib.util.spec_from_file_location(
    "fdp_par_commune",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fdp_par_commune.py"),
)
_fdp_mod = importlib.util.module_from_spec(_fdp_spec)
_fdp_spec.loader.exec_module(_fdp_mod)
FDPParCommune      = _fdp_mod.FDPParCommune
_LayerSelectorDialog = _fdp_mod._LayerSelectorDialog
del _fdp_spec, _fdp_mod

# ── build_displaced_sirene_layer ──────────────────────────────────────────────
_sd_spec = importlib.util.spec_from_file_location(
    "sirene_display",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sirene_display.py"),
)
_sd_mod = importlib.util.module_from_spec(_sd_spec)
_sd_spec.loader.exec_module(_sd_mod)
build_displaced_sirene_layer = _sd_mod.build_displaced_sirene_layer
del _sd_spec, _sd_mod

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
        all_groups = self._collect_groups(root)

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

            nom = commune["nom"]
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
                            # Chercher une couche polygone dans le groupe pour le déplacement
                            bati_layer = next(
                                (
                                    QgsProject.instance().mapLayer(ll.layerId())
                                    for ll in group.findLayers()
                                    if QgsProject.instance().mapLayer(ll.layerId())
                                    and isinstance(
                                        QgsProject.instance().mapLayer(ll.layerId()),
                                        QgsVectorLayer,
                                    )
                                    and QgsProject.instance().mapLayer(ll.layerId()).geometryType() == 2
                                ),
                                None,
                            )
                            if bati_layer:
                                feedback.pushInfo("   📌  Placement des établissements…")
                                sirene_layer = build_displaced_sirene_layer(
                                    sirene_layer, bati_layer, feedback
                                )
                            else:
                                feedback.pushInfo(
                                    "   ℹ  Pas de couche bâtiments trouvée — "
                                    "points SIRENE sans déplacement."
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

    def _collect_groups(self, node):
        """Collecte récursivement tous les QgsLayerTreeGroup sous node."""
        groups = []
        for child in node.children():
            if isinstance(child, QgsLayerTreeGroup):
                groups.append(child)
                groups.extend(self._collect_groups(child))
        return groups

    def _lookup_commune_batch(self, nom, insee_hint, feedback, warn_on_miss=False):
        """
        Résout un nom de commune en dict {nom, code, geometry} sans ouvrir
        de dialogue. Retourne None si introuvable ou ambigu.
        Priorité : custom property fdp_insee → lookup par nom exact.
        """
        _FIELDS = "fields=nom,code,contour&format=geojson&geometry=contour"
        _TYPES  = "type=commune-actuelle,arrondissement-municipal"
        _BASE   = "https://geo.api.gouv.fr/communes"

        def _get(url):
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            return r.json().get("features", [])

        def _to_dict(feat):
            p = feat["properties"]
            return {"nom": p["nom"], "code": p["code"], "geometry": feat["geometry"]}

        try:
            # 1. Lookup direct par code INSEE (fiable, commune déjà connue)
            if insee_hint:
                feats = _get(
                    f"{_BASE}?code={requests.utils.quote(insee_hint)}&{_FIELDS}&{_TYPES}"
                )
                if feats:
                    return _to_dict(feats[0])

            # 2. Lookup par nom
            feats = _get(
                f"{_BASE}?nom={requests.utils.quote(nom)}&{_FIELDS}&{_TYPES}"
            )
            if not feats:
                if warn_on_miss:
                    feedback.reportError(f"   ⚠  « {nom} » introuvable dans l'API Géo.", fatalError=False)
                return None

            # Correspondance exacte (insensible à la casse)
            for feat in feats:
                if feat["properties"]["nom"].lower() == nom.lower():
                    return _to_dict(feat)

            # Résultat unique non exact → on le prend quand même
            if len(feats) == 1:
                return _to_dict(feats[0])

            if warn_on_miss:
                feedback.reportError(
                    f"   ⚠  Plusieurs communes correspondent à « {nom} » — ignorée. "
                    "Ré-importez avec « FDP par Commune » pour fixer le code INSEE.",
                    fatalError=False,
                )
            return None

        except Exception as e:
            if warn_on_miss:
                feedback.reportError(f"   ⚠  API Géo ({nom}) : {e}", fatalError=False)
            return None
