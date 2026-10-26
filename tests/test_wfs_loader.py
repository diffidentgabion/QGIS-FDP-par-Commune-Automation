# -*- coding: utf-8 -*-
"""
Test de bout en bout, hors interface, du chargeur WFS de fdp_par_commune
(_load_wfs_layer / _load_code_insee_layer) : serveur Géoplateforme réel, puis
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
    T8  typename inexistant              → None + 1 avertissement « couche invalide »
                                           nommant le typename (~1 s, serveur interrogé)

Serveur muet (connexion ouverte, aucun octet — QGIS abandonne au bout de son
délai réseau, 60 s par défaut, et journalise « L'opération est annulée ») :
    T9   GetCapabilities muet une fois   → couche, même compte que T1
    T10  GetCapabilities muet à jamais   → EXCEPTION après 4 tentatives, jamais None
    T11  vérif. COUNT=1 muette à jamais  → couche valide, même type et compte que T1
                                           (QGIS relance 3 fois : 4 min perdues)
    T12  première page muette une fois   → relance interne de QGIS, même compte
    T13  DescribeFeatureType muet        → EXCEPTION après 4 tentatives, jamais None
Filtre code_insee (serveur réel) :
    T14  parcelles Paris 13e (75113)     → 4 641 via code_insee='75056' AND
                                           code_arr='113', sans repli, en secondes
    T15  IRIS Paris entier (75056)       → filtre vide : repli bbox IMMÉDIAT (pas
                                           4 min de « L'opération est annulée »)

Chaque silence coûte 60 s ou plus (T10, T11, T13 : 4 à 6 min). Ports distincts :
plusieurs tests peuvent tourner en parallèle, chacun dans son processus.
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
    QgsWkbTypes,
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
crs_2154 = QgsCoordinateReferenceSystem("EPSG:2154")
xform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), crs_2154, QgsProject.instance())


def commune(insee):
    """(bbox, couche contour) en EPSG:2154 — communes et arrondissements PLM."""
    feat = json.load(urllib.request.urlopen(
        f"https://geo.api.gouv.fr/communes/{insee}?type=commune-actuelle,"
        "arrondissement-municipal&fields=contour&format=geojson&geometry=contour"
    ))
    geom = alg._geojson_to_qgsgeometry(feat["geometry"])
    geom.transform(xform)
    return geom.boundingBox(), alg._geom_to_temp_layer(geom, "Polygon", crs_2154)


bbox, boundary = commune("72181")
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
        if "hang@" in faults:
            # Le GetCapabilities d'un port déjà utilisé lors d'un précédent
            # lancement serait servi par le cache réseau de QGIS : le silence
            # visé ne serait jamais atteint.
            mod.FDPParCommune._purge_network_cache()
    else:
        mod._WFS_URL = REAL_WFS
    print(f"\n=== {label}", flush=True)
    fb = _Feedback()
    t0 = time.time()
    try:
        out = fn(fb)
        res = ("layer", None) if out is None else (
            "layer", out.featureCount(), QgsWkbTypes.displayString(out.wkbType())
        )
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
        n_pages = sum(1 for l in lines if l.startswith("PASS") and "REQUEST=GetFeature" in l)
        n_inject = sum(1 for l in lines if l.startswith("INJECT"))
        n_hang = sum(1 for l in lines if l.startswith("HANG"))
        # Pannes réelles de Géoplateforme survenues pendant le test.
        n_real = sum(1 for l in lines if l.startswith(("UPSTREAM-FAIL", "PASS 5")))
        print(f"   mandataire : {n_pages} pages GetFeature servies + {n_inject} injectée(s)"
              f" + {n_hang} silence(s) + {n_real} panne(s) réelle(s)", flush=True)
    mod._WFS_URL = REAL_WFS


def bati(fb):
    return alg._load_wfs_layer("BDTOPO_V3:batiment", "Bâti", bbox, boundary, crs_2154, fb)


def parcelles(fb):
    return alg._load_code_insee_layer(
        "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle", "Parcelles cadastrales",
        "72181", bbox, boundary, crs_2154, fb,
    )


def parcelles_paris13(fb):
    b, lim = commune("75113")
    return alg._load_code_insee_layer(
        "CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle", "Parcelles cadastrales",
        "75113", b, lim, crs_2154, fb,
    )


def iris_paris(fb):
    b, lim = commune("75056")
    return alg._load_code_insee_layer(
        "STATISTICALUNITS.IRIS:contours_iris", "IRIS (quartiers INSEE)",
        "75056", b, lim, crs_2154, fb,
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
    "T9": ("T9 bâti, GetCapabilities muet une fois", bati, "hang@caps:1", 8805),
    "T10": ("T10 bâti, GetCapabilities muet à jamais", bati, "hang@caps:99", 8806),
    "T11": ("T11 bâti, vérification COUNT=1 muette à jamais", bati, "hang@probe:99", 8807),
    "T12": ("T12 bâti, première page muette une fois", bati, "hang@0:1", 8808),
    "T13": ("T13 bâti, DescribeFeatureType muet à jamais", bati, "hang@describe:99", 8809),
    "T14": ("T14 parcelles Paris 13e (arrondissement), serveur réel", parcelles_paris13, None, 8800),
    "T15": ("T15 IRIS Paris entier (filtre vide), serveur réel", iris_paris, None, 8800),
}
for tid in (sys.argv[1:] or TESTS):
    run(*TESTS[tid])

print("\n================ RÉSUMÉ")
for k, (res, warns) in results.items():
    print(f"{k:58s} {res}   avertissements : {len(warns)}")
app.exitQgis()
