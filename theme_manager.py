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

from qgis.core import (
    Qgis,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsMessageLog,
    QgsProject,
)
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

_SCOPE = "fdp_theme_manager"
_KEY = "state"

# Nombre de tentatives différées quand l'arbre du projet n'est pas (encore)
# exploitable — couvre les chargements de projet en cours et les signaux ratés.
_RETRY_BUDGET = 10


def _log(message, level=None):
    """Trace visible dans le panneau « Messages du journal » de QGIS."""
    if level is None:
        level = Qgis.Warning
    QgsMessageLog.logMessage(message, "FDP Thèmes", level)
    print(f"[theme_manager] {message}")


# =============================================================================
# Helpers
# =============================================================================


def _collect_commune_groups(group, result):
    """
    Un groupe-commune a des QgsLayerTreeLayer en enfants directs.
    Un groupe-lot n'a que des sous-groupes → on descend récursivement.
    """
    children = group.children()
    if any(isinstance(c, QgsLayerTreeLayer) for c in children):
        result.append(group)
    else:
        for child in children:
            if isinstance(child, QgsLayerTreeGroup):
                _collect_commune_groups(child, result)


def _commune_groups():
    """Retourne tous les groupes-communes, quel que soit leur niveau d'imbrication."""
    result = []
    for child in QgsProject.instance().layerTreeRoot().children():
        if isinstance(child, QgsLayerTreeGroup):
            _collect_commune_groups(child, result)
    return result


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
        super().__init__("Contrôle de visibilité", parent)
        self.setObjectName(self.OBJECT_NAME)
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        # tuple(chemin depuis la racine commune) → bool (True = visible)
        self._state: dict = {}

        # Racine d'arbre actuellement écoutée (peut changer entre projets)
        self._connected_root = None
        # Tentatives restantes quand le projet n'est pas encore prêt
        self._retry_budget = _RETRY_BUDGET

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

        btn_row = QHBoxLayout()
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

        Si le projet n'est pas encore exploitable (chargement en cours, signaux
        ratés), planifie automatiquement une nouvelle tentative — le panneau ne
        doit jamais rester vide alors que des communes sont chargées.
        """
        # La racine du projet a pu être remplacée : on suit toujours l'actuelle.
        self._ensure_root_signals()

        communes = _commune_groups()
        if not communes:
            self._schedule_retry()
            return

        union = _build_union_tree(communes)

        self._tree.blockSignals(True)
        try:
            self._tree.clear()
            self._populate(self._tree.invisibleRootItem(), union, (), communes)
            self._retry_budget = _RETRY_BUDGET
        except Exception:
            import traceback

            _log(
                "erreur lors de la construction de l'arbre :\n" + traceback.format_exc()
            )
            # L'arbre vient d'être vidé : ne pas le laisser vide, on réessaie.
            self._schedule_retry()
        finally:
            self._tree.blockSignals(False)

    def _schedule_retry(self):
        if self._retry_budget > 0:
            self._retry_budget -= 1
            self._debounce.start()

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
                visible = True
                for c in communes:
                    node = _follow_path(c, cpath)
                    if node is not None:
                        visible = node.itemVisibilityChecked()
                        break

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
        try:
            self._cascade_down(item, checked)
            self._bubble_up(item.parent())
        finally:
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
            name = item.text(0)
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
        try:
            self._set_subtree(self._tree.invisibleRootItem(), state)
        finally:
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
            item = parent.child(i)
            # Qt peut restituer le tuple stocké sous forme de liste → on force
            # le tuple, seul type utilisable comme clé de dict.
            cpath = tuple(item.data(0, Qt.UserRole) or ())
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
        project = QgsProject.instance()
        # Signaux au niveau projet — insensibles aux remplacements de la racine
        # de l'arbre, donc fiables quel que soit le cycle de vie du projet.
        project.layersAdded.connect(self._on_tree_changed)
        project.layersRemoved.connect(self._on_tree_changed)
        project.cleared.connect(self._on_project_cleared)
        project.readProject.connect(self._on_project_read)
        self._ensure_root_signals()

    def _ensure_root_signals(self):
        """
        (Re)branche addedChildren/removedChildren sur la racine COURANTE de
        l'arbre, sans jamais doubler une connexion. La racine peut être
        remplacée lors d'un clear/lecture de projet selon les versions de QGIS.
        """
        root = QgsProject.instance().layerTreeRoot()
        if root is self._connected_root:
            return
        old = self._connected_root
        if old is not None:
            try:
                old.addedChildren.disconnect(self._on_tree_changed)
                old.removedChildren.disconnect(self._on_tree_changed)
            except (TypeError, RuntimeError):
                pass  # déjà déconnecté ou objet C++ détruit
        root.addedChildren.connect(self._on_tree_changed)
        root.removedChildren.connect(self._on_tree_changed)
        self._connected_root = root

    def _on_tree_changed(self, *_):
        # Nouveau signal = nouvelles chances de trouver les communes.
        self._retry_budget = _RETRY_BUDGET
        self._debounce.start()

    def _on_debounce(self):
        self._refresh_and_apply()

    def _on_project_cleared(self, *_):
        """Vide l'arbre et reconnecte les signaux après ouverture d'un nouveau projet."""
        self._state.clear()
        self._tree.blockSignals(True)
        self._tree.clear()
        self._tree.blockSignals(False)
        self._retry_budget = _RETRY_BUDGET
        self._ensure_root_signals()

    def _on_project_read(self, *_):
        """Charge l'état persisté après ouverture complète du projet."""
        self._debounce.stop()
        self._retry_budget = _RETRY_BUDGET
        self._load_state()
        self._refresh_and_apply()

    def _refresh_and_apply(self):
        self._refresh_tree()
        self._apply_all()


# =============================================================================
# Auto-installation du hook de démarrage
# =============================================================================


def _install_startup_hook():
    """
    Écrit (une seule fois) un bloc dans startup.py du profil QGIS actif, de
    sorte que le dock s'ouvre automatiquement à chaque lancement de QGIS.

    Appelé automatiquement par ensure_theme_manager(). Requiert que ce module
    soit importé normalement (pas via exec()) pour que __file__ soit défini.
    Silencieux en cas d'erreur — ne bloque jamais QGIS.
    """
    import os
    import pathlib

    # __file__ n'est pas défini quand le script est chargé via exec() depuis
    # la console QGIS. On abandonne sans bruit dans ce cas.
    try:
        tool_dir = str(pathlib.Path(__file__).parent.resolve())
    except NameError:
        return

    try:
        from qgis.core import QgsApplication

        # qgisSettingsDirPath() retourne le dossier du profil actif, ex. :
        # C:\Users\…\AppData\Roaming\QGIS\QGIS3\profiles\default\
        profile_python = pathlib.Path(QgsApplication.qgisSettingsDirPath()) / "python"
    except Exception:
        profile_python = (
            pathlib.Path(os.environ.get("APPDATA", ""))
            / "QGIS"
            / "QGIS3"
            / "profiles"
            / "default"
            / "python"
        )

    try:
        profile_python.mkdir(parents=True, exist_ok=True)
        startup_path = profile_python / "startup.py"

        MARKER = "# >>> fdp_theme_manager"
        existing = (
            startup_path.read_text(encoding="utf-8") if startup_path.exists() else ""
        )
        if MARKER in existing:
            return  # Déjà installé

        block = f"""
{MARKER}
import sys as _sys
_fdp_tool_dir = r"{tool_dir}"
if _fdp_tool_dir not in _sys.path:
    _sys.path.insert(0, _fdp_tool_dir)

from qgis.PyQt.QtCore import QTimer as _QTimer
from qgis.core import QgsProject as _QgsProject

def _fdp_open_hook(*_):
    try:
        from qgis.utils import iface as _i
        if _i:
            from theme_manager import ensure_theme_manager
            ensure_theme_manager(_i)
    except Exception:
        pass

_QgsProject.instance().readProject.connect(_fdp_open_hook)
# Projet déjà ouvert au démarrage (restauration de session) : délai 1 s.
_QTimer.singleShot(1000, lambda: _fdp_open_hook() if _QgsProject.instance().fileName() else None)
# <<< fdp_theme_manager
"""
        sep = "\n\n" if existing.strip() else ""
        startup_path.write_text(existing + sep + block, encoding="utf-8")
    except Exception:
        pass  # Ne jamais bloquer QGIS


# =============================================================================
# Point d'entrée public
# =============================================================================


def _anchor(dock):
    """
    Garde une référence Python FORTE sur le dock dans qgis.utils (module
    immortel pendant toute la session QGIS).

    Sans cela, PyQt garbage-collecte la moitié Python du dock dès que le
    script Processing se termine : le widget C++ survit (parenté à la fenêtre
    principale, donc le panneau reste VISIBLE), mais toutes les connexions
    signal→slot Python meurent — arbre jamais peuplé, boutons inertes.
    C'était la cause du comportement intermittent.
    """
    try:
        import qgis.utils

        qgis.utils._fdp_theme_manager_dock = dock
    except Exception:
        pass


def ensure_theme_manager(iface):
    """
    Crée le dock s'il n'existe pas encore, ou le retrouve s'il a déjà été
    créé dans la session courante. Idempotent — sûr à appeler à chaque
    exécution du script Processing.

    Installe aussi silencieusement le hook startup.py pour que le dock
    s'ouvre automatiquement à tous les prochains lancements de QGIS.
    """
    main_win = iface.mainWindow()
    existing = main_win.findChild(QDockWidget, ThemeManagerDock.OBJECT_NAME)
    if existing is not None:
        try:
            if existing.widget() is None:
                raise RuntimeError("dock sans contenu")
            existing.show()
            existing.raise_()
            # Reconstruction systématique : un dock déjà ouvert a pu rater des
            # changements du projet (signaux perdus, projet rouvert, etc.).
            # Lève AttributeError si la moitié Python du dock a été perdue
            # (wrapper générique QDockWidget) → remplacement ci-dessous.
            existing._refresh_and_apply()
            _anchor(existing)
            _install_startup_hook()
            return existing
        except Exception:
            # Dock invalide (moitié Python garbage-collectée, widget détruit,
            # instance d'une ancienne version…) → on le remplace par un neuf.
            try:
                existing.close()
                existing.deleteLater()
            except Exception:
                pass
    dock = ThemeManagerDock(main_win)
    iface.addDockWidget(Qt.RightDockWidgetArea, dock)
    dock.show()
    _anchor(dock)
    _install_startup_hook()
    return dock
