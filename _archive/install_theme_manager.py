# -*- coding: utf-8 -*-
"""
install_theme_manager.py — Configuration unique par poste de travail

Exécuter UNE SEULE FOIS depuis la console Python de QGIS :

    exec(open(r"D:\Dropbox\SOFTWARE\000_DATAS QGIS\OUTIL IMPORTATION COMMUNE\QGIS-FDP-par-Commune-Automation\install_theme_manager.py").read())

Effet : QGIS écrira un startup.py dans le profil QGIS de la machine.
Après redémarrage de QGIS, le panneau Thèmes s'ouvre automatiquement
à chaque ouverture de projet — sans aucune manipulation supplémentaire.

Mise à jour de theme_manager.py dans Dropbox = mise à jour automatique
sur tous les postes (le startup.py pointe vers Dropbox, pas une copie locale).
"""

import os
import pathlib
import sys

# ── Dossier contenant ce script = dossier Dropbox du projet ──────────────────
# __file__ n'est pas défini quand le script est chargé via exec() depuis la
# console QGIS. Dans ce cas, on ouvre un sélecteur de dossier.
try:
    TOOL_DIR = str(pathlib.Path(__file__).parent.resolve())
except NameError:
    from qgis.PyQt.QtWidgets import QFileDialog
    TOOL_DIR = QFileDialog.getExistingDirectory(
        None,
        "Sélectionnez le dossier QGIS-FDP-par-Commune-Automation",
        os.path.expanduser("~"),
    )
    if not TOOL_DIR:
        raise SystemExit("Installation annulée.")

# ── Dossier python/ du profil QGIS actif ─────────────────────────────────────
try:
    from qgis.core import QgsApplication
    # qgisSettingsDirPath() retourne le dossier du profil actif, ex. :
    # C:\Users\…\AppData\Roaming\QGIS\QGIS3\profiles\default\
    profile_python = pathlib.Path(QgsApplication.qgisSettingsDirPath()) / "python"
except Exception:
    # Fallback : profil par défaut sous Windows
    profile_python = (
        pathlib.Path(os.environ["APPDATA"])
        / "QGIS" / "QGIS3" / "profiles" / "default" / "python"
    )

profile_python.mkdir(parents=True, exist_ok=True)
startup_path = profile_python / "startup.py"

# ── Contenu du bloc à injecter ────────────────────────────────────────────────
BLOCK_MARKER = "# >>> fdp_theme_manager"
BLOCK = f"""{BLOCK_MARKER}
import sys as _sys
_fdp_tool_dir = r"{TOOL_DIR}"
if _fdp_tool_dir not in _sys.path:
    _sys.path.insert(0, _fdp_tool_dir)

from qgis.PyQt.QtCore import QTimer as _QTimer
from qgis.core import QgsProject as _QgsProject

def _fdp_open_hook(*_):
    try:
        from qgis.utils import iface as _iface
        if _iface:
            from theme_manager import ensure_theme_manager
            ensure_theme_manager(_iface)
    except Exception:
        pass  # Ne jamais bloquer le démarrage de QGIS

_QgsProject.instance().readProject.connect(_fdp_open_hook)
# Projet déjà ouvert au démarrage (restauration de session) : délai 1 s.
_QTimer.singleShot(1000, lambda: _fdp_open_hook() if _QgsProject.instance().fileName() else None)
# <<< fdp_theme_manager
"""

# ── Vérification : déjà installé ? ───────────────────────────────────────────
existing = startup_path.read_text(encoding="utf-8") if startup_path.exists() else ""

if BLOCK_MARKER in existing:
    print(f"✅  Déjà installé sur ce poste ({startup_path})")
    print("    Aucune action nécessaire. Redémarrez QGIS si ce n'est pas déjà fait.")
else:
    separator = "\n\n" if existing.strip() else ""
    startup_path.write_text(existing + separator + BLOCK, encoding="utf-8")
    print(f"✅  Startup script configuré : {startup_path}")
    print()
    print("  → Redémarrez QGIS.")
    print("  → Le panneau Thèmes s'ouvrira automatiquement à chaque projet.")
    print()
    print("  Pour désinstaller, supprimez le bloc entre les marqueurs")
    print(f"  '# >>> fdp_theme_manager' dans {startup_path}")
