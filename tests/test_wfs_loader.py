# -*- coding: utf-8 -*-
"""
Test de bout en bout, hors interface, du chargeur WFS de fdp_par_commune
(_load_wfs_layer / _load_parcelle_layer) : serveur Géoplateforme réel, puis
pannes injectées par tests/wfs_fault_proxy.py.

Lancer avec le Python de QGIS (OSGeo4W) :
    python-qgis-ltr.bat tests/test_wfs_loader.py            # tous les tests
    python-qgis-ltr.bat tests/test_wfs_loader.py T2 T3      # une sélection

Attendu (Le Mans, INSEE 72181 — 19 pages de bâti) :
    T1  bâti réel                        → couche découpée, 0 avertissement
    T2  page 10000 corrompue une fois    → 1 avertissement, même compte que T1
    T3  page 10000 corrompue à jamais    → EXCEPTION après 4 tentatives
    T4  PREMIÈRE page corrompue une fois → 1 avertissement, même compte que T1
    T5  parcelles réelles                → couche, 0 avertissement (sauf panne réelle)
    T6  parcelles, page 5000 corrompue   → 1 avertissement, même compte que T5
    T7  couche vide sur l'emprise        → couche à 0 entité, pas d'exception
    T8  typename inexistant              → None + avertissement « couche invalide »
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

PREFIX = os.environ.get("QGIS_PREFIX_PATH", "")
sys.path.insert(0, os.path.join(PREFIX, "python", "plugins"))
from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProcessingFeedback,
    QgsProject,
)

QgsApplication.setPrefixPath(PREFIX, True)
app = QgsApplication([], True)
app.initQgis()
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_HERE)
PROXY = os.path.join(_HERE, "wfs_fault_proxy.py")
_spec = importlib.util.spec_from_file_location("fdp_par_commune", os.path.join(REPO, "fdp_par_commune.py"))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)
REAL_WFS = mod._WFS_URL


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


alg = mod.FDPParCommune()
feat = json.load(urllib.request.urlopen(
    "https://geo.api.gouv.fr/communes/72181?fields=contour&format=geojson&geometry=contour"
))
crs_2154 = QgsCoordinateReferenceSystem("EPSG:2154")
xform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), crs_2154, QgsProject.instance())
geom = alg._geojson_to_qgsgeometry(feat["geometry"])
geom.transform(xform)
bbox = geom.boundingBox()
boundary = alg._geom_to_temp_layer(geom, "Polygon", crs_2154)
print("Le Mans bbox:", bbox.toString(1), flush=True)

results = {}


def run(label, fn, faults=None, port=8800):
    proc = plog = None
    if faults is not None:
        plog_path = os.path.join(tempfile.gettempdir(), f"wfs_fault_proxy_{port}.log")
        plog = open(plog_path, "w")
        proc = subprocess.Popen([sys.executable, PROXY, str(port), faults],
                                stdout=plog, stderr=subprocess.STDOUT)
        time.sleep(2)
        mod._WFS_URL = f"http://127.0.0.1:{port}/wfs/ows"
    else:
        mod._WFS_URL = REAL_WFS
    print(f"\n=== {label}", flush=True)
    fb = _Feedback()
    t0 = time.time()
    try:
        out = fn(fb)
        res = ("layer", out.featureCount() if out is not None else None)
    except Exception as e:  # noqa: BLE001 — c'est l'issue attendue de T3
        res = ("EXCEPTION", str(e))
    print(f"   -> {res}  avertissements={len(fb.warnings())}  {time.time() - t0:.1f}s", flush=True)
    results[label] = (res, fb.warnings())
    if proc:
        proc.terminate()
        time.sleep(1)
        plog.close()
        with open(plog_path) as f:
            lines = f.read().splitlines()
        n_pages = sum(1 for l in lines if "REQUEST=GetFeature" in l and "RESULTTYPE" not in l)
        n_inject = sum(1 for l in lines if l.startswith("INJECT"))
        print(f"   mandataire : {n_pages} pages GetFeature servies + {n_inject} injectée(s)", flush=True)
    mod._WFS_URL = REAL_WFS


def bati(fb):
    return alg._load_wfs_layer("BDTOPO_V3:batiment", "Bâti", bbox, boundary, crs_2154, fb)


def parcelles(fb):
    return alg._load_parcelle_layer(
        "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle", "Parcelles cadastrales",
        "72181", bbox, boundary, crs_2154, fb,
    )


def piste(fb):
    return alg._load_wfs_layer("BDTOPO_V3:piste_d_aerodrome", "Piste d'aérodrome", bbox, boundary, crs_2154, fb)


def bogus(fb):
    return alg._load_wfs_layer("BDTOPO_V3:n_existe_pas", "Bogus", bbox, boundary, crs_2154, fb)


TESTS = {
    "T1": ("T1 bâti, serveur réel", bati, None, 8800),
    "T2": ("T2 bâti, page 10000 corrompue une fois", bati, "10000:1", 8801),
    "T3": ("T3 bâti, page 10000 corrompue à jamais", bati, "10000:99", 8802),
    "T4": ("T4 bâti, PREMIÈRE page corrompue une fois", bati, "0:1", 8803),
    "T5": ("T5 parcelles, serveur réel", parcelles, None, 8800),
    "T6": ("T6 parcelles, page 5000 corrompue une fois", parcelles, "5000:1", 8804),
    "T7": ("T7 piste d'aérodrome (vide sur l'emprise), serveur réel", piste, None, 8800),
    "T8": ("T8 typename inexistant, serveur réel", bogus, None, 8800),
}
for tid in (sys.argv[1:] or TESTS):
    run(*TESTS[tid])

print("\n================ RÉSUMÉ")
for k, (res, warns) in results.items():
    print(f"{k:58s} {res}   avertissements : {len(warns)}")
app.exitQgis()
