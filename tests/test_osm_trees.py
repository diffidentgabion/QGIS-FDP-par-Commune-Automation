# -*- coding: utf-8 -*-
"""
Test de bout en bout, hors interface, de la couche « Arbres (OSM) » de
fdp_par_commune (_overpass_request / _overpass_fetch / _prefetch_osm_trees /
_load_osm_trees / _apply_osm_arbres_style).

Les pannes sont injectées en remplaçant la seule méthode qui touche au réseau,
_overpass_request — pas de mandataire à lancer.

Lancer avec le Python de QGIS (OSGeo4W) :
    python-qgis-ltr.bat tests/test_osm_trees.py            # tous les tests
    python-qgis-ltr.bat tests/test_osm_trees.py T1 T4      # une sélection

Attendu (Autun, INSEE 71014 — quelques centaines d'arbres) :
    T1  Overpass réel                     → couche découpée en 2154, 0 avertissement
    T2  3 arbres injectés                 → attributs mappés, tags absents = NULL
    T3  requête réelle bas niveau         → HTTP 200 + JSON via QgsBlockingNetworkRequest
    T4  champ « remark »                  → ÉCHEC (jamais de couche tronquée)
    T5  page HTML servie en HTTP 200      → ÉCHEC après épuisement des tentatives
    T6  504 puis miroir OK                → couche livrée, 1 avertissement
    T7  aucun arbre sur la commune        → None, sans exception
    T8  symbologie                        → 3 règles ponctuelles, palette BD Forêt
    T9  dialogue : styles règle-par-règle → l'éditeur ne crée pas de style vide
    T10 disjoncteur                       → plus AUCUN appel réseau après un échec
    T11 emprise groupée                   → 2 communes = 1 seule requête
    T12 communes dispersées               → pas de regroupement, aucun appel
"""
import importlib.util
import json
import os
import sys
import time

import requests as _real_requests

PREFIX = os.environ.get("QGIS_PREFIX_PATH", "")
sys.path.insert(0, os.path.join(PREFIX, "python", "plugins"))
from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProcessingFeedback,
    QgsProject,
    QgsRectangle,
    QgsRuleBasedRenderer,
    QgsSymbol,
)

QgsApplication.setPrefixPath(PREFIX, True)
app = QgsApplication([], True)
app.initQgis()
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_HERE)
_spec = importlib.util.spec_from_file_location(
    "fdp_par_commune", os.path.join(REPO, "fdp_par_commune.py")
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class _Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.lines = []

    def pushInfo(self, s):
        self.lines.append(("I", s))
        print("      I", s, flush=True)

    def pushWarning(self, s):
        self.lines.append(("W", s))
        print("      W", s, flush=True)

    def warnings(self):
        return [s for k, s in self.lines if k == "W"]


# ── Emprise de test : Autun (71014), quelques centaines d'arbres dans OSM ────
alg = mod.FDPParCommune()
feat = _real_requests.get(
    "https://geo.api.gouv.fr/communes/71014",
    params={"fields": "nom,contour", "format": "geojson", "geometry": "contour"},
    timeout=30,
).json()
crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
crs_2154 = QgsCoordinateReferenceSystem("EPSG:2154")
xform = QgsCoordinateTransform(crs_4326, crs_2154, QgsProject.instance())
geom = alg._geojson_to_qgsgeometry(feat["geometry"])
inside = geom.pointOnSurface().asPoint()  # point garanti DANS la commune (4326)
geom.transform(xform)
bbox = geom.boundingBox()
boundary = alg._geom_to_temp_layer(geom, "Polygon", crs_2154)
print(f"{feat['properties']['nom']} bbox:", bbox.toString(1), flush=True)


# ── Faux réseau : remplace _overpass_request(url, query, feedback) ───────────
class _FakeNetwork:
    """Sert une réponse par tentative ; la dernière est répétée si besoin."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, query, feedback):
        self.calls.append((url, query))
        i = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[i]


def _ok(payload):
    return (200, json.dumps(payload), None)


def _http(status):
    return (status, None, f"HTTP {status}")


def _tree(osm_id, dlon, dlat, tags):
    return {
        "type": "node",
        "id": osm_id,
        "lon": inside.x() + dlon,
        "lat": inside.y() + dlat,
        "tags": tags,
    }


_THREE_TREES = {
    "elements": [
        _tree(1, 0.0000, 0.0000, {"natural": "tree", "leaf_type": "broadleaved",
                                  "genus": "Tilia", "species:fr": "Tilleul",
                                  "height": "12 m"}),
        _tree(2, 0.0001, 0.0000, {"natural": "tree", "leaf_type": "needleleaved"}),
        _tree(3, 0.0000, 0.0001, {"natural": "tree"}),
    ]
}
_HTML_ERROR = "<html><body>Error: runtime error: open64 …</body></html>"

results = {}
net = None


def run(label, fn, responses=None):
    """Exécute `fn(feedback)`, éventuellement avec un faux réseau."""
    global net
    net = None
    # État porté par l'instance entre deux communes : à remettre à zéro, sinon
    # le disjoncteur ou le cache d'un test fausserait le suivant.
    alg._overpass_down = False
    alg._osm_trees_prefetch = None
    if responses is not None:
        net = _FakeNetwork(responses)
        alg._overpass_request = net
    # Délais de classe = 2/5 s : inutile d'attendre en test.
    alg._OVERPASS_RETRY_DELAYS = (0, 0)
    print(f"\n=== {label}", flush=True)
    fb = _Feedback()
    t0 = time.time()
    try:
        res = ("ok", fn(fb))
    except Exception as e:  # noqa: BLE001
        res = ("EXCEPTION", str(e))
    print(f"   -> {res}  avertissements={len(fb.warnings())}  {time.time() - t0:.1f}s",
          flush=True)
    if net is not None:
        print(f"   appels réseau : {len(net.calls)} "
              f"{[u.split('/')[2] for u, _ in net.calls]}", flush=True)
    results[label] = (res, fb.warnings())
    for attr in ("_overpass_request", "_OVERPASS_RETRY_DELAYS"):
        if attr in alg.__dict__:
            del alg.__dict__[attr]
    return res


def _load(fb):
    return alg._load_osm_trees(bbox, boundary, crs_2154, fb)


# ── T1 — Overpass réel ───────────────────────────────────────────────────────
def t1(fb):
    layer = _load(fb)
    assert layer is not None, "aucune couche renvoyée"
    assert layer.crs().authid() == "EPSG:2154", layer.crs().authid()
    assert layer.featureCount() > 0, "couche vide"
    names = [f.name() for f in layer.fields()]
    for expected in ("osm_id", "leaf_type", "species_fr", "circumference"):
        assert expected in names, f"champ {expected} absent de {names}"
    assert geom.boundingBox().contains(layer.extent()), "entités hors emprise"
    return f"{layer.featureCount()} arbres, champs OK, découpage OK"


# ── T2 — mappage des attributs ───────────────────────────────────────────────
def t2(fb):
    layer = _load(fb)
    assert layer is not None and layer.featureCount() == 3, layer
    by_id = {f["osm_id"]: f for f in layer.getFeatures()}
    assert by_id[1]["leaf_type"] == "broadleaved", by_id[1]["leaf_type"]
    assert by_id[1]["species_fr"] == "Tilleul", "« : » doit devenir « _ »"
    assert by_id[1]["height"] == "12 m", "valeur libre conservée telle quelle"
    assert by_id[2]["leaf_type"] == "needleleaved"
    assert not by_id[3]["leaf_type"], "tag absent → attribut vide, pas d'erreur"
    assert isinstance(by_id[1]["osm_id"], int), type(by_id[1]["osm_id"])
    return "3 arbres, attributs mappés"


# ── T3 — couche réseau bas niveau, serveur réel ──────────────────────────────
def t3(fb):
    query = alg._osm_trees_query(bbox, crs_2154)
    status, text, problem = alg._overpass_request(mod._OVERPASS_URLS[0], query, fb)
    fb.pushInfo(f"statut={status} panne={problem}")
    assert problem is None, problem
    assert status == 200, status
    data = json.loads(text)
    assert "elements" in data, list(data)
    assert not data.get("remark"), data.get("remark")
    return f"HTTP 200, {len(data['elements'])} éléments via QgsBlockingNetworkRequest"


# ── T4 / T5 / T6 / T7 — pannes ───────────────────────────────────────────────
def t4(fb):
    layer = _load(fb)
    assert layer is None, "une réponse « remark » ne doit JAMAIS donner de couche"
    assert any("partielle" in w for w in fb.warnings()), fb.warnings()
    assert alg._overpass_down is True, "le disjoncteur doit être armé"
    return "échec correctement détecté"


def t5(fb):
    layer = _load(fb)
    assert layer is None, layer
    assert any("illisible" in w for w in fb.warnings()), fb.warnings()
    return "page HTML en HTTP 200 rejetée"


def t6(fb):
    layer = _load(fb)
    assert layer is not None and layer.featureCount() == 3, layer
    assert len(fb.warnings()) == 1, fb.warnings()
    assert len(net.calls) == 2, net.calls
    assert net.calls[0][0] != net.calls[1][0], "la reprise doit changer de serveur"
    return "bascule sur le miroir, couche livrée"


def t7(fb):
    layer = _load(fb)
    assert layer is None, "commune sans arbre → pas de couche vide dans la légende"
    assert not fb.warnings(), "un rural sans arbre n'est pas une anomalie"
    return "aucun arbre → None sans avertissement"


# ── T8 — symbologie ──────────────────────────────────────────────────────────
def t8(fb):
    layer = _load(fb)
    alg._apply_style(layer, "osm_arbres")
    renderer = layer.renderer()
    assert isinstance(renderer, QgsRuleBasedRenderer), type(renderer)
    rules = renderer.rootRule().children()
    assert [r.label() for r in rules] == ["Feuillu", "Conifère", "Essence non précisée"]
    colors = [r.symbol().color().name() for r in rules]
    assert colors == ["#8ccd8c", "#5fa878", "#74bc82"], colors
    for r in rules:
        assert r.symbol().type() == QgsSymbol.Marker, "symbole ponctuel attendu"
        assert r.symbol().symbolLayer(0).strokeColor().name() == "#4a8a5e"
    assert rules[-1].filterExpression() in ("ELSE", ""), rules[-1].filterExpression()
    return f"{len(rules)} règles ponctuelles, palette BD Forêt"


# ── T9 — le dialogue ne doit pas fabriquer de style vide ─────────────────────
def t9(fb):
    dlg = mod._LayerSelectorDialog()
    cat = {e["style_key"]: e for e in mod._LAYER_CATALOGUE}
    for sk in ("osm_arbres", "bdforet", "vegetation", "rpg_parcelles"):
        dlg._rebuild_style_editor(cat[sk])
        assert sk not in dlg._styles, (
            f"{sk} : l'éditeur a créé un style vide — à l'import, {{}} n'étant "
            "pas None, _apply_custom_style écraserait le rendu par règles"
        )
    dlg._rebuild_style_editor(cat["buildings"])
    assert dlg._styles.get("buildings"), dlg._styles.get("buildings")
    dlg.deleteLater()
    return "styles calculés protégés, style éditable intact"


# ── T10 — disjoncteur : un lot ne doit pas repayer l'échec ───────────────────
def t10(fb):
    assert _load(fb) is None, "premier appel : échec attendu"
    n_after_first = len(net.calls)
    assert n_after_first == 3, f"3 tentatives attendues, {n_after_first}"
    for _ in range(5):  # 5 communes suivantes du lot
        assert _load(fb) is None
    assert len(net.calls) == n_after_first, (
        f"le disjoncteur n'a pas tenu : {len(net.calls)} appels au total"
    )
    return f"{n_after_first} appels puis plus rien pour 5 communes"


# ── T11 — emprise groupée : deux communes, une seule requête ─────────────────
def t11(fb):
    voisine = QgsRectangle(bbox)
    voisine.setXMinimum(bbox.xMinimum() + 4000)
    voisine.setXMaximum(bbox.xMaximum() + 4000)
    alg._prefetch_osm_trees([bbox, voisine], fb)
    assert alg._osm_trees_prefetch is not None, "aucune emprise groupée créée"
    assert len(net.calls) == 1, net.calls
    for _ in range(3):  # trois communes servies depuis le cache
        layer = _load(fb)
        assert layer is not None and layer.featureCount() == 3, layer
    assert len(net.calls) == 1, f"{len(net.calls)} appels — le cache n'a pas servi"
    return "1 requête pour 2 communes déclarées + 3 découpages"


# ── T12 — communes trop dispersées : pas de regroupement ─────────────────────
def t12(fb):
    lointaine = QgsRectangle(bbox)
    lointaine.setXMinimum(bbox.xMinimum() + 400000)  # 400 km plus à l'est
    lointaine.setXMaximum(bbox.xMaximum() + 400000)
    alg._prefetch_osm_trees([bbox, lointaine], fb)
    assert alg._osm_trees_prefetch is None, "regroupement à ne pas faire"
    assert not net.calls, net.calls
    assert any("dispersées" in i for _k, i in fb.lines), fb.lines
    return "regroupement refusé, aucun appel réseau"


_REMARK = {"remark": "runtime error: Query timed out",
           "elements": [_tree(1, 0, 0, {})]}

TESTS = {
    "T1": ("T1 Overpass réel (Autun)", t1, None),
    "T2": ("T2 mappage des attributs", t2, [_ok(_THREE_TREES)]),
    "T3": ("T3 requête réelle bas niveau", t3, None),
    "T4": ("T4 réponse partielle (champ remark)", t4, [_ok(_REMARK)]),
    "T5": ("T5 page HTML servie en HTTP 200", t5, [(200, _HTML_ERROR, None)]),
    "T6": ("T6 504 puis miroir OK", t6, [_http(504), _ok(_THREE_TREES)]),
    "T7": ("T7 commune sans arbre", t7, [_ok({"elements": []})]),
    "T8": ("T8 symbologie", t8, [_ok(_THREE_TREES)]),
    "T9": ("T9 dialogue — styles règle-par-règle", t9, [_ok(_THREE_TREES)]),
    "T10": ("T10 disjoncteur sur un lot", t10, [_http(504)]),
    "T11": ("T11 emprise groupée", t11, [_ok(_THREE_TREES)]),
    "T12": ("T12 communes trop dispersées", t12, [_ok(_THREE_TREES)]),
}
for tid in (sys.argv[1:] or TESTS):
    run(*TESTS[tid])

print("\n================ RÉSUMÉ")
ko = 0
for k, (res, warns) in results.items():
    ok = res[0] != "EXCEPTION"
    ko += 0 if ok else 1
    print(f"{'✓' if ok else '✗'} {k:38s} {res[1]}   avertissements : {len(warns)}")
print(f"\n{len(results) - ko}/{len(results)} test(s) OK")
app.exitQgis()
sys.exit(1 if ko else 0)
