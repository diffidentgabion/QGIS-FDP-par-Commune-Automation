# -*- coding: utf-8 -*-
"""
Test hors interface de l'année de construction du bâti (bdnb_age +
_enrich_bati_annee) et des divisions infra-communales (IRIS, QPV).

Les pannes sont injectées en remplaçant la seule méthode qui touche au réseau,
FDPParCommune._http_get — pas de mandataire à lancer.

Lancer avec le Python de QGIS (OSGeo4W) :
    python-qgis-ltr.bat tests/test_age_divisions.py           # tous les tests
    python-qgis-ltr.bat tests/test_age_divisions.py T1 T5     # une sélection

Attendu :
    T1  _annee_from_date                  → QDate, texte, NULL, valeurs folles
    T2  BDNB réel (dep 69, 2 cleabs)      → années attendues, cache de session
    T3  BDNB en panne                     → BdnbError + disjoncteur armé
    T4  enrichissement d'une couche Bâti  → union BDNB + BD TOPO, alias posés
    T5  QPV réel (Villeurbanne)           → 5 quartiers en 2154, étiquetés
    T6  QPV sur une commune sans quartier → None, sans exception
    T7  catalogue et aiguillages          → ordre de légende et styles
    T8  en-tête Range                     → HTTP 206 honoré par _http_get
    T9  IRIS réel (Villeurbanne)          → 48 IRIS, filtre code_insee, étiquetés
"""
import importlib.util
import os
import sys

PREFIX = os.environ.get("QGIS_PREFIX_PATH", "")
sys.path.insert(0, os.path.join(PREFIX, "python", "plugins"))
from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsProcessingFeedback,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDate, QMetaType  # noqa: E402

QgsApplication.setPrefixPath(PREFIX, True)
app = QgsApplication([], True)
app.initQgis()
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_HERE)


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO, name + ".py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load("fdp_par_commune")
bdnb = _load("bdnb_age")

# Bâtiments témoins du département 69, vérifiés à la main sur l'export BDNB.
CLEABS_2020 = "BATIMENT0000002274951543"  # groupe bdnb-bg-7H81-CAQD-3P7X → 2020
CLEABS_1800 = "BATIMENT0000000239713342"  # groupe bdnb-bg-DEGR-HEU7-SBM3 → 1800
CLEABS_ABSENT = "BATIMENT0000000000000001"  # inconnu de la BDNB


class _Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.lines = []
        self.warnings = []

    def pushInfo(self, s):
        self.lines.append(s)

    def pushWarning(self, s):
        self.warnings.append(s)

    def reportError(self, s, fatalError=False):
        self.warnings.append(s)


def _bati_layer():
    """Couche mémoire aux champs BD TOPO utiles (cleabs + date_d_apparition)."""
    layer = QgsVectorLayer("Polygon?crs=EPSG:2154", "Bâti", "memory")
    provider = layer.dataProvider()
    provider.addAttributes(
        [
            QgsField("cleabs", QMetaType.Type.QString),
            QgsField("date_d_apparition", QMetaType.Type.QString),
        ]
    )
    layer.updateFields()
    rows = [
        (CLEABS_2020, None),          # BDNB seule
        (CLEABS_1800, "1800-01-01Z"),  # les deux sources concordent
        (CLEABS_ABSENT, "1984-01-01Z"),  # BD TOPO seule
        ("BATIMENT0000000000000002", None),  # aucune source
    ]
    feats = []
    for cleabs, date in rows:
        f = QgsFeature(layer.fields())
        f.setAttributes([cleabs, date])
        feats.append(f)
    provider.addFeatures(feats)
    layer.updateExtents()
    return layer


results = {}


def run(label, fn):
    fb = _Feedback()
    try:
        detail = fn(fb)
        results[label] = ("OK", detail, fb.warnings)
    except Exception as exc:  # noqa: BLE001 — un test qui lève est un échec
        results[label] = ("EXCEPTION", f"{type(exc).__name__}: {exc}", fb.warnings)
    print(f"  {label}: {results[label][0]} — {results[label][1]}")


# ── T1 ───────────────────────────────────────────────────────────────────────
def t1(fb):
    f = mod.FDPParCommune._annee_from_date
    assert f(QDate(1984, 1, 1)) == 1984, "QDate"
    assert f("1820-01-01Z") == 1820, "texte ISO"
    assert f(None) is None, "None"
    assert f("NULL") is None, "NULL textuel"
    assert f("") is None, "chaîne vide"
    assert f("0800-01-01") is None, "année invraisemblable"
    return "5 formes acceptées, 1 rejetée"


# ── T2 ───────────────────────────────────────────────────────────────────────
def t2(fb):
    bdnb._MEMBER_CACHE.clear()
    alg = mod.FDPParCommune()
    annees = bdnb.annees_construction(
        "69", {CLEABS_2020, CLEABS_1800, CLEABS_ABSENT}, alg._http_get, fb
    )
    assert annees.get(CLEABS_2020) == 2020, annees
    assert annees.get(CLEABS_1800) == 1800, annees
    assert CLEABS_ABSENT not in annees, "cleabs inconnu présent"
    assert ("69" in str(list(bdnb._MEMBER_CACHE)[0])), "cache de session non peuplé"
    # Deuxième appel : doit être servi par le cache, sans réseau.
    alg._http_get = lambda *a, **k: (None, None, "réseau interdit")
    again = bdnb.annees_construction("69", {CLEABS_2020}, alg._http_get, fb)
    assert again.get(CLEABS_2020) == 2020, "cache non utilisé"
    return f"{len(annees)} année(s), cache réutilisé"


# ── T3 ───────────────────────────────────────────────────────────────────────
def t3(fb):
    bdnb._MEMBER_CACHE.clear()
    bdnb._millesime_cache = bdnb.MILLESIME_FALLBACK  # pas d'appel data.gouv
    alg = mod.FDPParCommune()
    alg._http_get = lambda *a, **k: (503, None, "HTTP 503")
    raised = False
    try:
        bdnb.annees_construction("69", {CLEABS_2020}, alg._http_get, fb)
    except bdnb.BdnbError:
        raised = True
    assert raised, "BdnbError non levée"

    layer = _bati_layer()
    alg._enrich_bati_annee(layer, "69001", fb)
    assert getattr(alg, "_bdnb_down", False), "disjoncteur non armé"
    assert any("BDNB indisponible" in w for w in fb.warnings), fb.warnings
    i_annee = layer.fields().indexOf("annee_construction")
    annees = [f[i_annee] for f in layer.getFeatures()]
    # Repli : seules les dates BD TOPO subsistent (1800 et 1984).
    assert sorted(a for a in annees if a) == [1800, 1984], annees

    # Deuxième commune du lot : plus aucun appel réseau (disjoncteur).
    fb2 = _Feedback()
    alg._http_get = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("réseau rappelé après panne")
    )
    alg._enrich_bati_annee(_bati_layer(), "69001", fb2)
    assert any("déjà indisponible" in l for l in fb2.lines), fb2.lines
    return "panne isolée, repli BD TOPO, disjoncteur actif"


# ── T4 ───────────────────────────────────────────────────────────────────────
def t4(fb):
    bdnb._MEMBER_CACHE.clear()
    bdnb._millesime_cache = None
    alg = mod.FDPParCommune()
    layer = _bati_layer()
    alg._enrich_bati_annee(layer, "69001", fb)
    i_annee = layer.fields().indexOf("annee_construction")
    i_source = layer.fields().indexOf("source_annee")
    assert i_annee != -1 and i_source != -1, "champs non créés"
    par_cleabs = {
        f["cleabs"]: (f[i_annee], f[i_source]) for f in layer.getFeatures()
    }
    assert par_cleabs[CLEABS_2020] == (2020, "BDNB"), par_cleabs
    assert par_cleabs[CLEABS_1800][1] == "BDNB", par_cleabs
    assert par_cleabs[CLEABS_ABSENT] == (1984, "BD TOPO"), par_cleabs
    assert not par_cleabs["BATIMENT0000000000000002"][0], par_cleabs
    assert layer.attributeAlias(i_annee) == "Année de construction", "alias absent"
    assert "annee_construction" in layer.mapTipTemplate(), "infobulle absente"
    assert not fb.warnings, fb.warnings
    return "3/4 bâtiments datés (2 BDNB, 1 BD TOPO), alias et infobulle posés"


# ── T5 ───────────────────────────────────────────────────────────────────────
def t5(fb):
    alg = mod.FDPParCommune()
    crs = QgsCoordinateReferenceSystem("EPSG:2154")
    layer = alg._load_qpv_layer("69266", crs, fb)  # Villeurbanne
    assert layer is not None and layer.isValid(), "couche QPV absente"
    assert layer.featureCount() == 5, layer.featureCount()
    assert layer.crs().authid() == "EPSG:2154", layer.crs().authid()
    assert layer.labelsEnabled(), "étiquettes désactivées"
    noms = {f["lib_qp"] for f in layer.getFeatures()}
    assert "Tonkin" in noms, noms
    assert layer.fields().indexOf("code_qp") != -1, "attributs perdus"
    # Archive réutilisée pour la commune suivante, sans nouveau téléchargement.
    assert os.path.exists(alg._qpv_zip), "archive non mise en cache"
    return f"{layer.featureCount()} quartiers : {sorted(noms)[0]}…"


# ── T6 ───────────────────────────────────────────────────────────────────────
def t6(fb):
    alg = mod.FDPParCommune()
    layer = alg._load_qpv_layer("69001", QgsCoordinateReferenceSystem("EPSG:2154"), fb)
    assert layer is None, "couche vide créée"
    assert any("Aucun quartier prioritaire" in l for l in fb.lines), fb.lines
    return "None sur une commune sans QPV"


# ── T7 ───────────────────────────────────────────────────────────────────────
def t7(fb):
    noms = [e["display_name"] for e in mod._LAYER_CATALOGUE]
    keys = [e["style_key"] for e in mod._LAYER_CATALOGUE]
    zones = [i for i, e in enumerate(mod._LAYER_CATALOGUE) if e["section"] == "zones"]
    i_qpv, i_iris = keys.index("qpv"), keys.index("iris")
    # L'ordre du catalogue est haut → bas dans la légende.
    assert i_qpv == max(zones) + 1, "QPV pas immédiatement sous les zonages"
    assert i_iris == i_qpv + 1, "IRIS pas sous les QPV"
    assert i_iris < keys.index("sirene"), "divisions sous le fond de plan"
    assert noms[i_qpv] == "Quartiers prioritaires (QPV)", noms[i_qpv]
    for key in ("qpv", "iris"):
        assert mod._DEFAULT_STYLES.get(key), f"style par défaut manquant : {key}"
    # QPV hors WFS, IRIS via le chargeur à filtre code_insee.
    assert mod._LAYER_CATALOGUE[i_qpv]["typename"] is None, "QPV traité comme WFS"
    assert mod._LAYER_CATALOGUE[i_iris]["typename"].endswith("contours_iris")
    assert "contours_iris" in " ".join(mod._LABEL_FIELDS), mod._LABEL_FIELDS
    # Les arbres OSM restent juste au-dessus de la végétation haute.
    assert keys.index("osm_arbres") < keys.index("bdforet"), "arbres sous la végétation"
    assert mod._LAYER_CATALOGUE[keys.index("osm_arbres")]["checked"], "arbres décochés"
    return "QPV sous les zonages, IRIS sous les QPV, arbres cochés"


# ── T8 ───────────────────────────────────────────────────────────────────────
def t8(fb):
    alg = mod.FDPParCommune()
    url = bdnb.dep_zip_url(bdnb.MILLESIME_FALLBACK, "69")
    status, content, panne = alg._http_get(url, fb, headers={"Range": "bytes=0-99"})
    assert panne is None, panne
    assert status == 206, status
    assert len(content) == 100, len(content)
    return "Range honoré (206, 100 octets)"


# ── T9 ───────────────────────────────────────────────────────────────────────
def t9(fb):
    from qgis.core import QgsCoordinateTransform, QgsProject

    alg = mod.FDPParCommune()
    crs = QgsCoordinateReferenceSystem("EPSG:2154")
    commune = alg._lookup_commune_batch("Villeurbanne", "69266", fb)
    assert commune is not None, "commune non résolue"
    geom = alg._geojson_to_qgsgeometry(commune["geometry"])
    geom.transform(
        QgsCoordinateTransform(
            QgsCoordinateReferenceSystem("EPSG:4326"), crs, QgsProject.instance()
        )
    )
    boundary = alg._geom_to_temp_layer(geom, "Polygon", crs)
    layer = alg._load_code_insee_layer(
        "STATISTICALUNITS.IRIS:contours_iris",
        "IRIS (quartiers INSEE)",
        "69266",
        geom.boundingBox(),
        boundary,
        crs,
        fb,
    )
    assert layer is not None and layer.isValid(), "couche IRIS absente"
    assert layer.featureCount() == 48, layer.featureCount()
    assert layer.crs().authid() == "EPSG:2154", layer.crs().authid()
    assert layer.labelsEnabled(), "étiquettes IRIS désactivées"
    assert layer.labeling().settings().fieldName == "nom_iris", "champ étiqueté"
    noms = {f["nom_iris"] for f in layer.getFeatures()}
    assert "Charmettes" in noms, sorted(noms)[:5]
    return f"{layer.featureCount()} IRIS étiquetés sur nom_iris"


TESTS = {
    "T1": ("T1 _annee_from_date", t1),
    "T2": ("T2 BDNB réel (dep 69)", t2),
    "T3": ("T3 BDNB en panne", t3),
    "T4": ("T4 enrichissement du bâti", t4),
    "T5": ("T5 QPV réel (Villeurbanne)", t5),
    "T6": ("T6 commune sans QPV", t6),
    "T7": ("T7 catalogue et aiguillages", t7),
    "T8": ("T8 en-tête Range", t8),
    "T9": ("T9 IRIS via filtre code_insee", t9),
}
for tid in (sys.argv[1:] or TESTS):
    label, fn = TESTS[tid]
    run(label, fn)

print("\n================ RÉSUMÉ")
ko = 0
for label, (state, detail, warns) in results.items():
    ok = state == "OK"
    ko += 0 if ok else 1
    print(f"{'✓' if ok else '✗'} {label:34s} {detail}   avertissements : {len(warns)}")
print(f"\n{len(results) - ko}/{len(results)} test(s) OK")
app.exitQgis()
sys.exit(1 if ko else 0)
