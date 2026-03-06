# -*- coding: utf-8 -*-
"""
theme_manager.py — Gestionnaire de thèmes visuels pour FDP par Commune

Expose :
    THEME_DEFINITIONS   list[dict]  — source unique de vérité pour les thèmes
    ensure_theme_manager(iface) -> ThemeManagerDock
        Crée ou retrouve le dock persistant dans la session QGIS courante.

Utilisation depuis fdp_par_commune.py :
    from qgis.utils import iface
    ensure_theme_manager(iface)

Ajouter un thème : ajouter un dict à THEME_DEFINITIONS. Rien d'autre.
"""

from functools import partial

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer, QgsProject


# =============================================================================
# Source unique de vérité — définitions des thèmes
# =============================================================================
# Chaque thème est un dict avec :
#   name     str         Nom affiché dans le dock
#   patterns list[dict]  Règles de correspondance (voir _layer_matches)
#
# Deux types de pattern — les deux ciblent des COUCHES FEUILLES uniquement :
#
#   {"type": "layer", "value": "..."}
#       Correspond à toute couche dont le nom CONTIENT "value" (insensible à la casse).
#
#   {"type": "group", "value": "..."}
#       Correspond à toute couche dont l'un des GROUPES ANCÊTRES est nommé
#       exactement "value" (insensible à la casse).
#       N.B. : on remonte la hiérarchie plutôt que de tester le nœud groupe
#       directement, ce qui contourne un bug de binding Python/QGIS 3.28 sur
#       QgsLayerTreeGroup.name().
#
# Ajouter un thème = ajouter un dict ici. Rien d'autre ne change.

THEME_DEFINITIONS = [
    {
        "name": "Éducation",
        "patterns": [
            # Couches dans le groupe ZAI "Science et enseignement" (Ecole maternelle, Collège…)
            {"type": "group", "value": "Science et enseignement"},
            # Sous-couches SIRENE × bâtiments : "Bâti — Éducation — Collège", etc.
            {"type": "layer", "value": "Bâti — Éducation"},
        ],
    },
    # ── Exemples de thèmes futurs (décommenter et compléter au besoin) ────────
    # {
    #     "name": "Voirie",
    #     "patterns": [
    #         {"type": "layer", "value": "Voirie"},
    #         {"type": "layer", "value": "Voie ferrée"},
    #         {"type": "group", "value": "Équipements de transport"},
    #     ],
    # },
    # {
    #     "name": "Bâti intrinsèque",
    #     "patterns": [
    #         {"type": "group", "value": "Bâti intrinsèque"},
    #         {"type": "group", "value": "Bâti — Données"},
    #     ],
    # },
    # {
    #     "name": "Hydrographie",
    #     "patterns": [
    #         {"type": "group", "value": "Hydrographie"},
    #     ],
    # },
]


# =============================================================================
# Helpers — correspondance couches / thèmes
# =============================================================================

def _ancestor_group_names(layer_node):
    """
    Retourne l'ensemble des noms (en minuscules) de tous les groupes ancêtres
    d'un QgsLayerTreeLayer, en remontant jusqu'à la racine.

    On remonte via node.parent() plutôt que d'inspecter les nœuds groupes
    directement, ce qui évite d'appeler QgsLayerTreeGroup.name() depuis la
    boucle principale (contournement d'un bug de binding QGIS 3.28).
    """
    names = set()
    node = layer_node.parent()
    while node is not None:
        try:
            n = node.name()
            if n:                      # root name is "" — skip it
                names.add(n.lower())
        except Exception:
            pass   # nœud sans nom valide — on ignore et on continue
        try:
            node = node.parent()
        except Exception:
            break
    return names


def _layer_matches(layer_node, patterns):
    """
    Retourne True si la couche feuille correspond à l'un des patterns.

    N'accepte que des QgsLayerTreeLayer — les groupes sont ignorés.
    Cette contrainte résout un TypeError sur QgsLayerTreeGroup.name() dans
    certaines versions de QGIS 3.28.
    """
    if not isinstance(layer_node, QgsLayerTreeLayer):
        return False

    layer = layer_node.layer()

    for pat in patterns:
        kind  = pat["type"]
        value = pat["value"].lower()

        if kind == "layer":
            # Correspondance sur le nom de la couche (sous-chaîne, insensible casse).
            if layer and value in layer.name().lower():
                return True

        elif kind == "group":
            # Correspondance sur le nom d'un groupe ancêtre.
            if value in _ancestor_group_names(layer_node):
                return True

    return False


def _collect_layers(node):
    """
    Retourne une LISTE de tous les nœuds feuilles (QgsLayerTreeLayer) de l'arbre.

    On utilise une liste (pas un générateur) pour prendre un instantané complet
    de l'arbre AVANT de modifier la visibilité des couches. Sans cela, chaque
    appel à setItemVisibilityChecked() déclenche des signaux Qt qui peuvent
    modifier l'arbre en cours d'itération et provoquer un access violation.
    """
    result = []
    if isinstance(node, QgsLayerTreeLayer):
        result.append(node)
    else:
        for child in node.children():
            result.extend(_collect_layers(child))
    return result


def apply_all_themes(active_theme_names):
    """
    Applique la visibilité de toutes les couches en mode exclusif.

    active_theme_names : set[str] — noms des thèmes actuellement cochés

    Mode exclusif : quand au moins un thème est actif, seules les couches
    réclamées par un thème actif restent visibles — toutes les autres sont
    masquées. Quand aucun thème n'est actif, toutes les couches sont restaurées.

    Ce mode permet de passer instantanément d'une vue thématique à une autre
    sans avoir à masquer manuellement les couches au préalable.
    """
    root  = QgsProject.instance().layerTreeRoot()
    # Snapshot complet avant toute modification — évite l'invalidation du
    # parcours par les signaux déclenchés par setItemVisibilityChecked().
    nodes = _collect_layers(root)

    if not active_theme_names:
        # Aucun thème actif → tout afficher (état neutre)
        for layer_node in nodes:
            layer_node.setItemVisibilityChecked(True)
        return

    # Au moins un thème actif → mode exclusif
    for layer_node in nodes:
        visible = any(
            theme["name"] in active_theme_names
            and _layer_matches(layer_node, theme["patterns"])
            for theme in THEME_DEFINITIONS
        )
        layer_node.setItemVisibilityChecked(visible)


# =============================================================================
# Dock widget persistant
# =============================================================================

class ThemeManagerDock(QDockWidget):
    """
    Panneau latéral persistant — survit aux ré-exécutions du script Processing.

    Contient :
    - Un bouton "Tout afficher"  → rend toutes les couches visibles + décoche thèmes
    - Un bouton "Tout masquer"   → masque toutes les couches   + décoche thèmes
    - Une case à cocher par thème (combinables librement)
    """

    OBJECT_NAME = "fdp_theme_manager"

    def __init__(self, parent=None):
        super().__init__("Thèmes cartographiques", parent)
        self.setObjectName(self.OBJECT_NAME)
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        # État interne : nom_thème → booléen
        self._state = {t["name"]: False for t in THEME_DEFINITIONS}

        # Références aux cases à cocher (maintient les objets en vie + accès direct)
        self._checkboxes = {}
        # Références aux partial callbacks — nécessaire pour éviter leur collecte
        # par le GC de Python (PyQt5 ne garde qu'une référence faible aux callables)
        self._handlers = []

        # Timer anti-rebond : évite de rappeler apply_all_themes() une fois par
        # couche lors du chargement en bloc d'une commune.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self._reapply)

        self._build_ui()
        self._connect_project_signals()

    # -------------------------------------------------------------------------
    # Construction de l'interface
    # -------------------------------------------------------------------------

    def _build_ui(self):
        container = QWidget()
        main_vbox = QVBoxLayout(container)
        main_vbox.setContentsMargins(8, 8, 8, 8)
        main_vbox.setSpacing(6)

        title = QLabel("<b>Thèmes</b>")
        main_vbox.addWidget(title)

        btn_row  = QHBoxLayout()
        btn_show = QPushButton("Tout afficher")
        btn_hide = QPushButton("Tout masquer")
        btn_show.setToolTip("Rend toutes les couches visibles et décoche les thèmes")
        btn_hide.setToolTip("Masque toutes les couches et décoche les thèmes")
        btn_show.clicked.connect(self._on_show_all)
        btn_hide.clicked.connect(self._on_hide_all)
        btn_row.addWidget(btn_show)
        btn_row.addWidget(btn_hide)
        main_vbox.addLayout(btn_row)

        sep = QLabel()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: #cccccc;")
        main_vbox.addWidget(sep)

        scroll_area   = QScrollArea()
        scroll_widget = QWidget()
        scroll_vbox   = QVBoxLayout(scroll_widget)
        scroll_vbox.setContentsMargins(0, 4, 0, 4)
        scroll_vbox.setSpacing(4)

        for theme in THEME_DEFINITIONS:
            cb = QCheckBox(theme["name"])
            cb.setChecked(False)
            # partial() fige le nom du thème dans le callback sans dépendre de
            # sender() (qui retourne None pour les slots Python sans @pyqtSlot).
            # La référence est conservée dans self._handlers pour éviter le GC.
            handler = partial(self._on_theme_toggled, theme["name"])
            self._handlers.append(handler)
            cb.toggled.connect(handler)
            self._checkboxes[theme["name"]] = cb
            scroll_vbox.addWidget(cb)

        scroll_vbox.addStretch()
        scroll_area.setWidget(scroll_widget)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.NoFrame)
        main_vbox.addWidget(scroll_area)

        self.setWidget(container)

    # -------------------------------------------------------------------------
    # Connexion aux signaux du projet
    # -------------------------------------------------------------------------

    def _connect_project_signals(self):
        """
        S'abonne à addedChildren sur la racine de l'arbre pour ré-appliquer
        les thèmes actifs dès qu'une nouvelle commune est chargée.
        """
        root = QgsProject.instance().layerTreeRoot()
        root.addedChildren.connect(self._on_tree_changed)

        # Reconnexion si un nouveau projet est ouvert dans la même session.
        QgsProject.instance().cleared.connect(self._on_project_cleared)

    def _on_project_cleared(self, *args):
        """Reconnecte addedChildren après ouverture d'un nouveau projet."""
        root = QgsProject.instance().layerTreeRoot()
        root.addedChildren.connect(self._on_tree_changed)

    # -------------------------------------------------------------------------
    # Gestionnaires de signaux
    # -------------------------------------------------------------------------

    def _on_tree_changed(self, *args):
        """
        Appelé à chaque ajout de nœud dans l'arbre.
        Le timer anti-rebond garantit un seul recalcul après le chargement
        en bloc de toutes les couches d'une commune.
        """
        if any(self._state.values()):
            self._debounce.start()

    def _on_theme_toggled(self, theme_name, checked):
        """Appelé par partial() quand l'utilisateur coche/décoche un thème."""
        self._state[theme_name] = checked
        self._reapply()

    def _reapply(self):
        """Applique l'état courant des thèmes à l'arbre des couches."""
        active = {name for name, on in self._state.items() if on}
        apply_all_themes(active)

    # -------------------------------------------------------------------------
    # Actions globales
    # -------------------------------------------------------------------------

    def _on_show_all(self, _checked=False):
        """Rend toutes les couches visibles et décoche les thèmes."""
        self._uncheck_all_silently()
        for layer_node in _collect_layers(QgsProject.instance().layerTreeRoot()):
            layer_node.setItemVisibilityChecked(True)

    def _on_hide_all(self, _checked=False):
        """
        Masque toutes les couches et décoche les thèmes.
        Point de départ typique avant d'activer des thèmes un par un.
        """
        self._uncheck_all_silently()
        for layer_node in _collect_layers(QgsProject.instance().layerTreeRoot()):
            layer_node.setItemVisibilityChecked(False)

    def _uncheck_all_silently(self):
        """Décoche toutes les cases sans déclencher _on_checkbox_toggled."""
        for name, cb in self._checkboxes.items():
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
            self._state[name] = False


# =============================================================================
# Point d'entrée public
# =============================================================================

def ensure_theme_manager(iface):
    """
    Crée le dock ThemeManagerDock s'il n'existe pas encore, ou le retrouve
    s'il a déjà été créé lors d'une exécution précédente dans la même session.
    Sûr à appeler à chaque exécution du script Processing — idempotent.
    """
    main_win = iface.mainWindow()

    existing = main_win.findChild(QDockWidget, ThemeManagerDock.OBJECT_NAME)
    if existing is not None:
        # Discard a previously broken dock (empty widget = failed __init__)
        if existing.widget() is None:
            existing.close()
            existing.deleteLater()
        else:
            existing.show()
            existing.raise_()
            return existing

    dock = ThemeManagerDock(main_win)
    iface.addDockWidget(Qt.RightDockWidgetArea, dock)
    dock.show()
    return dock
