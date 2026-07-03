# -*- coding: utf-8 -*-
"""
fdp_import_lot.py — Import en lot de nouvelles communes

Apparaît dans la boîte à outils Processing comme :
    "FDP — Import en lot (nouvelles communes)"

L'utilisateur saisit une liste de noms ou codes INSEE (un par ligne).
Le dialogue de sélection des couches est affiché UNE SEULE FOIS, puis
chaque commune est importée automatiquement avec les mêmes réglages.
"""

import importlib.util
import os

from qgis.core import QgsProcessingParameterString
from qgis.PyQt.QtWidgets import QDialog

# ── Charger FDPParCommune et _LayerSelectorDialog depuis fdp_par_commune.py ──
_fdp_spec = importlib.util.spec_from_file_location(
    "fdp_par_commune",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fdp_par_commune.py"),
)
_fdp_mod = importlib.util.module_from_spec(_fdp_spec)
_fdp_spec.loader.exec_module(_fdp_mod)
FDPParCommune        = _fdp_mod.FDPParCommune
_LayerSelectorDialog = _fdp_mod._LayerSelectorDialog
del _fdp_spec, _fdp_mod


class FDPImportEnLot(FDPParCommune):
    """
    Importe plusieurs nouvelles communes en une seule exécution.
    Le dialogue de sélection des couches s'ouvre une fois ; les mêmes
    réglages (couches, styles, topographie) s'appliquent à toutes.
    """

    COMMUNES_PARAM = "COMMUNES"

    def name(self):
        return "fdp_import_lot"

    def displayName(self):
        return "FDP — Import en lot (nouvelles communes)"

    def createInstance(self):
        return FDPImportEnLot()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterString(
                self.COMMUNES_PARAM,
                "Communes à importer (une par ligne — nom ou code INSEE)",
                multiLine=True,
                defaultValue="",
            )
        )

    # =========================================================================

    def processAlgorithm(self, parameters, context, feedback):
        raw = self.parameterAsString(parameters, self.COMMUNES_PARAM, context)
        names = [line.strip() for line in raw.splitlines() if line.strip()]
        if not names:
            raise Exception("Aucune commune saisie.")

        total = len(names)
        feedback.pushInfo(f"📋  {total} commune(s) à importer.")

        # ── Sélection des couches — affiché une seule fois ────────────────────
        dlg = _LayerSelectorDialog()
        if dlg.exec_() != QDialog.Accepted:
            raise Exception("Annulé.")
        selected_entries = dlg.result_layers
        if not selected_entries and dlg.topo_config is None:
            raise Exception("Aucune couche sélectionnée.")
        topo_config = dlg.topo_config

        ok_count   = 0
        skip_count = 0

        # ── 1. Résoudre toutes les communes (API Géo) avant l'import ──────────
        resolved = []
        for nom_input in names:
            if feedback.isCanceled():
                break
            feedback.pushInfo(f"🔎  Recherche de « {nom_input} »…")
            commune = self._lookup_commune_batch(
                nom_input, None, feedback, warn_on_miss=True
            )
            if commune is None:
                skip_count += 1
                continue
            resolved.append(commune)

        # ── 2. Pré-chargement SIRENE en un seul balayage (si SIRENE demandé) ──
        # Évite de rebalayer le parquet national par commune (voir docs/PERF.md).
        if resolved and any(e["style_key"] == "sirene" for e in selected_entries):
            self._prefetch_sirene([c["code"] for c in resolved], feedback)

        # ── 3. Import de chaque commune ───────────────────────────────────────
        total_ok = len(resolved)
        for i, commune in enumerate(resolved):
            if feedback.isCanceled():
                break

            nom   = commune["nom"]
            insee = commune["code"]
            dep   = self._get_dep(insee)
            feedback.pushInfo(f"\n[{i + 1}/{total_ok}]  {nom} ({dep}) — INSEE {insee}")
            feedback.setProgress(int(100 * i / max(total_ok, 1)))

            try:
                self._run_commune_import(
                    commune, selected_entries, topo_config, feedback,
                    show_save_dialog=False,
                )
                ok_count += 1
            except Exception as e:
                feedback.reportError(
                    f"   ⚠  Échec ({nom}) : {e}", fatalError=False
                )
                skip_count += 1

        feedback.setProgress(100)
        feedback.pushInfo(
            f"\n✅  {ok_count} commune(s) importée(s)"
            + (f", {skip_count} ignorée(s)." if skip_count else ".")
        )
        return {}
