# -*- coding: utf-8 -*-
"""
theme_manager.py — Gestionnaire de thèmes visuels pour FDP par Commune

Expose :
    ensure_theme_manager(iface) -> ThemeManagerDock

Le panneau reflète l'UNION de la structure de toutes les communes chargées.
Si un groupe ou une couche n'existe que dans certaines communes, il apparaît
quand même — et est simplement ignoré pour les communes qui ne l'ont pas.

Cocher un groupe → tous ses enfants deviennent visibles dans chaque commune.
Décocher un groupe → tous ses enfants deviennent invisibles dans chaque commune.
Un groupe dont les enfants sont partiellement cochés affiche un état mixte (—).

L'état est persisté dans le .qgz et restauré à l'ouverture du projet.
Rien à configurer : ajouter une commune charge automatiquement sa structure.
"""

import json

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsLayerTreeGroup, QgsProject

_SCOPE = "fdp_theme_manager"
_KEY   = "state"


# =============================================================================
# Helpers
# =============================================================================

def _commune_groups():
    """Nœuds groupe de premier niveau = groupes communes."""
    return [
        c for c in QgsProject.instance().layerTreeRoot().children()
        if isinstance(c, QgsLayerTreeGroup)
    ]


def _find_child(qgs_node, name):
    for child in qgs_node.children():
        if child.name() == name:
            return child
    return None


def _follow_path(qgs_node, path):
    """Descend dans qgs_node en suivant path. Retourne le nœud final ou None."""
    node = qgs_node
    for name in path:
        node = _find_child(node, name)
        if node is None:
            return None
    return node


def _build_union_tree(communes):
    """
    Construit un dict arbre union depuis toutes les communes.
    Structure : {nom: {nom_enfant: {...}, ...}, ...} — récursif.
    Un dict vide signifie feuille (couche sans enfants).
    L'ordre d'insertion (Python 3.7+) préserve l'ordre des couches.
    """
    root = {}
    for commune in communes:
        _union_merge(commune, root)
    return root


def _union_merge(qgs_node, d):
    for child in qgs_node.children():
        name = child.name()
        if name not in d:
            d[name] = {}
        if isinstance(child, QgsLayerTreeGroup):
            _union_merge(child, d[name])


# =============================================================================
# Dock
# =============================================================================

class ThemeManagerDock(QDockWidget):
    """
    Panneau latéral persistant — survit aux ré-exécutions du script Processing.

    Chaque nœud de l'arbre correspond à un groupe ou une couche dans les
    communes chargées. Les modifications s'appliquent à TOUTES les communes.
    """

    OBJECT_NAME = "fdp_theme_manager"

    def __init__(self, parent=None):
        super().__init__("Thèmes cartographiques", parent)
        self.setObjectName(self.OBJECT_NAME)
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        # tuple(chemin depuis la racine commune) → bool (True = visible)
        self._state: dict = {}

        # Debounce : un seul recalcul après chargement en bloc d'une commune
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self._on_debounce)

        self._build_ui()
        self._load_state()
        self._connect_signals()
        # Différé : des communes peuvent déjà être chargées dans le projet
        QTimer.singleShot(0, self._refresh_and_apply)

    # ── Construction de l'interface ───────────────────────────────────────────

    def _build_ui(self):
        root_w = QWidget()
        vbox = QVBoxLayout(root_w)
        vbox.setContentsMargins(8, 8, 8, 8)
        vbox.setSpacing(6)

        btn_row  = QHBoxLayout()
        btn_show = QPushButton("Tout afficher")
        btn_hide = QPushButton("Tout masquer")
        btn_show.clicked.connect(self._on_show_all)
        btn_hide.clicked.connect(self._on_hide_all)
        btn_row.addWidget(btn_show)
        btn_row.addWidget(btn_hide)
        vbox.addLayout(btn_row)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setAnimated(True)
        self._tree.setIndentation(16)
        self._tree.setAlternatingRowColors(True)
        self._tree.itemChanged.connect(self._on_item_changed)
        vbox.addWidget(self._tree)

        self.setWidget(root_w)

    # ── Construction de l'arbre ───────────────────────────────────────────────

    def _refresh_tree(self):
        """
        Reconstruit l'arbre depuis l'UNION de toutes les communes.
        Préserve les états connus. Ajoute les nouveaux nœuds à True par défaut.
        """
        communes = _commune_groups()
        if not communes:
            return

        self._tree.blockSignals(True)
        self._tree.clear()
        union = _build_union_tree(communes)
        self._populate(self._tree.invisibleRootItem(), union, (), communes)
        self._tree.blockSignals(False)

    def _populate(self, parent_item, union_dict, path, communes):
        for name, children in union_dict.items():
            cpath = path + (name,)

            item = QTreeWidgetItem(parent_item, [name])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setData(0, Qt.UserRole, cpath)

            # État sauvegardé prioritaire ; sinon visibilité actuelle dans la
            # première commune qui possède ce nœud ; sinon True par défaut.
            if cpath in self._state:
                visible = self._state[cpath]
            else:
                qgs_node = next(
                    (n for c in communes if (n := _follow_path(c, cpath)) is not None),
                    None,
                )
                visible = qgs_node.itemVisibilityChecked() if qgs_node else True

            item.setCheckState(0, Qt.Checked if visible else Qt.Unchecked)

            if children:
                self._populate(item, children, cpath, communes)

    # ── Logique des cases à cocher ────────────────────────────────────────────

    def _on_item_changed(self, item, column):
        if column != 0:
            return
        state = item.checkState(0)
        if state == Qt.PartiallyChecked:
            # Mis à jour en cascade remontante (programmatique) — on ignore
            return

        checked = state == Qt.Checked

        self._tree.blockSignals(True)
        self._cascade_down(item, checked)
        self._bubble_up(item.parent())
        self._tree.blockSignals(False)

        self._collect_state()
        self._apply_all()
        self._save_state()

    def _cascade_down(self, item, checked):
        """Propage l'état coché/décoché à tous les descendants."""
        s = Qt.Checked if checked else Qt.Unchecked
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(0, s)
            self._cascade_down(child, checked)

    def _bubble_up(self, item):
        """Met à jour l'état des ancêtres (tout coché / tout décoché / mixte)."""
        if item is None:
            return
        states = {item.child(i).checkState(0) for i in range(item.childCount())}
        if states == {Qt.Checked}:
            item.setCheckState(0, Qt.Checked)
        elif states == {Qt.Unchecked}:
            item.setCheckState(0, Qt.Unchecked)
        else:
            item.setCheckState(0, Qt.PartiallyChecked)
        self._bubble_up(item.parent())

    # ── Application à QGIS ────────────────────────────────────────────────────

    def _apply_all(self):
        """Applique l'état de l'arbre à toutes les communes chargées."""
        root_item = self._tree.invisibleRootItem()
        for commune in _commune_groups():
            self._apply_node(commune, root_item)

    def _apply_node(self, qgs_node, tree_parent):
        """Parcours récursif : applique chaque item de l'arbre au nœud QGIS correspondant."""
        for i in range(tree_parent.childCount()):
            item = tree_parent.child(i)
            name    = item.text(0)
            # PartiallyChecked → le groupe lui-même est visible (enfants gérés récursivement)
            visible = item.checkState(0) != Qt.Unchecked

            qgs_child = _find_child(qgs_node, name)
            if qgs_child is None:
                continue

            qgs_child.setItemVisibilityChecked(visible)

            if isinstance(qgs_child, QgsLayerTreeGroup) and item.childCount() > 0:
                self._apply_node(qgs_child, item)

    # ── Boutons globaux ───────────────────────────────────────────────────────

    def _on_show_all(self):
        self._set_all(True)

    def _on_hide_all(self):
        self._set_all(False)

    def _set_all(self, visible):
        state = Qt.Checked if visible else Qt.Unchecked
        self._tree.blockSignals(True)
        self._set_subtree(self._tree.invisibleRootItem(), state)
        self._tree.blockSignals(False)
        self._collect_state()
        self._apply_all()
        self._save_state()

    def _set_subtree(self, parent, state):
        for i in range(parent.childCount()):
            item = parent.child(i)
            item.setCheckState(0, state)
            self._set_subtree(item, state)

    # ── Gestion de l'état ─────────────────────────────────────────────────────

    def _collect_state(self):
        """Lit l'arbre et met à jour self._state."""
        self._state.clear()
        self._collect_from(self._tree.invisibleRootItem())

    def _collect_from(self, parent):
        for i in range(parent.childCount()):
            item  = parent.child(i)
            cpath = item.data(0, Qt.UserRole)
            self._state[cpath] = item.checkState(0) != Qt.Unchecked
            self._collect_from(item)

    # ── Persistance ───────────────────────────────────────────────────────────

    def _save_state(self):
        serial = {"|".join(p): v for p, v in self._state.items()}
        QgsProject.instance().writeEntry(_SCOPE, _KEY, json.dumps(serial))

    def _load_state(self):
        raw, _ = QgsProject.instance().readEntry(_SCOPE, _KEY, "")
        if not raw:
            return
        try:
            saved = json.loads(raw)
        except (ValueError, TypeError):
            return
        self._state = {tuple(k.split("|")): bool(v) for k, v in saved.items()}

    # ── Signaux projet ────────────────────────────────────────────────────────

    def _connect_signals(self):
        QgsProject.instance().layerTreeRoot().addedChildren.connect(
            self._on_tree_changed
        )
        QgsProject.instance().cleared.connect(self._on_project_cleared)
        QgsProject.instance().readProject.connect(self._on_project_read)

    def _on_tree_changed(self, *_):
        self._debounce.start()

    def _on_debounce(self):
        self._refresh_and_apply()

    def _on_project_cleared(self, *_):
        """Vide l'arbre et reconnecte addedChildren après ouverture d'un nouveau projet."""
        self._state.clear()
        self._tree.blockSignals(True)
        self._tree.clear()
        self._tree.blockSignals(False)
        QgsProject.instance().layerTreeRoot().addedChildren.connect(
            self._on_tree_changed
        )

    def _on_project_read(self, *_):
        """Charge l'état persisté après ouverture complète du projet."""
        self._debounce.stop()
        self._load_state()
        self._refresh_and_apply()

    def _refresh_and_apply(self):
        self._refresh_tree()
        self._apply_all()


# =============================================================================
# Point d'entrée public
# =============================================================================

def ensure_theme_manager(iface):
    """
    Crée le dock s'il n'existe pas encore, ou le retrouve s'il a déjà été
    créé dans la session courante. Idempotent — sûr à appeler à chaque
    exécution du script Processing.
    """
    main_win = iface.mainWindow()
    existing = main_win.findChild(QDockWidget, ThemeManagerDock.OBJECT_NAME)
    if existing is not None:
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
