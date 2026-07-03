# -*- coding: utf-8 -*-
"""
fdp_gestionnaire_themes.py — Ouverture du panneau Contrôle de visibilité

Apparaît dans la boîte à outils Processing comme :
    "FDP — Gestionnaire de thèmes"

Ouvre (ou ramène au premier plan) le panneau de contrôle de visibilité des
couches par thème, sans avoir à passer par la console Python. Le même
panneau s'ouvre aussi automatiquement à la fin d'un import FDP par Commune.

Installation : identique à fdp_par_commune.py — le dossier contenant ce
fichier doit être déclaré comme dossier de scripts Processing.
"""

import importlib.util
import os

from qgis.core import QgsProcessingAlgorithm

# ── Charger ensure_theme_manager depuis theme_manager.py ─────────────────────
# Les scripts Processing QGIS n'ont pas de __package__ défini, donc les imports
# relatifs échouent. On charge le fichier voisin via importlib avec son chemin
# absolu, ce qui fonctionne quel que soit l'emplacement du script.
_tm_spec = importlib.util.spec_from_file_location(
    "theme_manager",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme_manager.py"),
)
_tm_mod = importlib.util.module_from_spec(_tm_spec)
_tm_spec.loader.exec_module(_tm_mod)
ensure_theme_manager = _tm_mod.ensure_theme_manager
del _tm_spec, _tm_mod


class FDPGestionnaireThemes(QgsProcessingAlgorithm):
    """Ouvre le panneau de contrôle de visibilité des couches par thème."""

    # ── Métadonnées Processing ────────────────────────────────────────────────

    def flags(self):
        # FlagNoThreading oblige QGIS à exécuter cet algorithme dans le thread
        # principal de Qt, indispensable pour créer/afficher un QDockWidget.
        return super().flags() | QgsProcessingAlgorithm.FlagNoThreading

    def name(self):
        return "fdp_gestionnaire_themes"

    def displayName(self):
        return "FDP — Gestionnaire de thèmes"

    def group(self):
        return "Fond de Plan"

    def groupId(self):
        return "fond_de_plan"

    def shortHelpString(self):
        return (
            "Ouvre le panneau « Contrôle de visibilité » qui permet d'afficher "
            "ou masquer les couches par thème dans TOUTES les communes chargées.\n\n"
            "Le panneau reflète l'union de la structure de toutes les communes "
            "du projet ; il se met à jour automatiquement quand une commune est "
            "ajoutée ou retirée. L'état est enregistré dans le projet .qgz."
        )

    def createInstance(self):
        return FDPGestionnaireThemes()

    def initAlgorithm(self, config=None):
        pass  # Aucun paramètre : un clic sur Exécuter ouvre le panneau.

    # ── Point d'entrée ────────────────────────────────────────────────────────

    def processAlgorithm(self, parameters, context, feedback):
        from qgis.utils import iface

        if iface is None:
            raise Exception(
                "Interface QGIS indisponible (exécution headless ?) — "
                "le panneau nécessite la fenêtre principale de QGIS."
            )

        ensure_theme_manager(iface)
        feedback.pushInfo("✅  Panneau « Contrôle de visibilité » ouvert.")
        return {}
