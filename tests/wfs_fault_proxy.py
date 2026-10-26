# -*- coding: utf-8 -*-
"""
Mandataire local d'injection de pannes pour le WFS Géoplateforme.

Relaie toute requête vers https://data.geopf.fr telle quelle, sauf les
requêtes visées par une panne, dont les N premières occurrences échouent
puis passent normalement. Deux pannes :

  STARTINDEX:N       GetFeature de ce STARTINDEX → dump de métriques
                     Prometheus/JMX capturé (HTTP 200, text/plain) —
                     exactement ce que la passerelle Géoplateforme renvoie
                     lors d'un mauvais routage ;
  hang@CIBLE:N       serveur muet : connexion tenue ouverte sans un octet
                     pendant HANG_S secondes (> délai réseau de QGIS, 60 s
                     par défaut), puis fermée. CIBLE : caps (GetCapabilities),
                     describe (DescribeFeatureType), probe (GetFeature
                     COUNT=1 — la vérification que QGIS fait à la création de
                     la couche) ou un STARTINDEX (page, hors COUNT=1).

Les réponses GetCapabilities sont réécrites pour que les requêtes suivantes
de QGIS (GetFeature, DescribeFeatureType) transitent aussi par ce mandataire.

Usage :  python wfs_fault_proxy.py PORT [PANNE,PANNE…]
         (durée du silence : variable d'environnement WFS_PROXY_HANG_S, 75 s)
Voir docs/WFS ROBUSTESSE.md et tests/test_wfs_loader.py.
"""
import http.client
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
FAULTS = {}  # STARTINDEX → nb de pages Prometheus restant à injecter
HANGS = {}   # cible → nb de silences restant à infliger
for spec in (sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else []):
    target, n = spec.rsplit(":", 1)
    if target.startswith("hang@"):
        HANGS[target[len("hang@"):]] = int(n)
    else:
        FAULTS[target] = int(n)
HANG_S = float(os.environ.get("WFS_PROXY_HANG_S", "75"))

_HERE = os.path.dirname(os.path.abspath(__file__))
PROM_BODY = open(os.path.join(_HERE, "fixtures", "geopf_misroute_prometheus.txt"), "rb").read()
UPSTREAM = "https://data.geopf.fr"
LOCAL = f"http://127.0.0.1:{PORT}"
_lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silencieux
        pass

    def do_GET(self):
        query = {
            k.upper(): v[0]
            for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()
        }
        # QGIS omet STARTINDEX sur la première page : on la traite comme « 0 ».
        start_index = query.get("STARTINDEX", "0")
        count = query.get("COUNT") or query.get("MAXFEATURES")
        request = query.get("REQUEST", "").lower()
        is_getfeature = (
            request == "getfeature"
            and query.get("RESULTTYPE", "").lower() != "hits"
        )
        if request == "getcapabilities":
            target = "caps"
        elif request == "describefeaturetype":
            target = "describe"
        elif is_getfeature:
            target = "probe" if count == "1" else start_index
        else:
            target = None
        with _lock:
            hang = target is not None and HANGS.get(target, 0) > 0
            if hang:
                HANGS[target] -= 1
            inject = not hang and is_getfeature and FAULTS.get(start_index, 0) > 0
            if inject:
                FAULTS[start_index] -= 1
        if hang:
            print(f"HANG {target} {HANG_S:.0f} s (reste {HANGS[target]})", flush=True)
            time.sleep(HANG_S)
            self.close_connection = True
            return
        if inject:
            print(f"INJECT prometheus STARTINDEX={start_index} (reste {FAULTS[start_index]})", flush=True)
            # Mêmes en-têtes de cache que la vraie passerelle (Kong renvoie
            # « private, max-age=1814400 ») — une nouvelle couche QGIS émet
            # malgré tout une requête réseau, ce que ce test vérifie.
            self._send(200, "text/plain; version=0.0.4; charset=utf-8", PROM_BODY,
                       extra={"Cache-Control": "private, max-age=1814400"})
            return

        req = urllib.request.Request(
            UPSTREAM + self.path,
            headers={"Accept-Encoding": "identity", "User-Agent": "wfs-fault-proxy"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body, ctype, status = r.read(), r.headers.get("Content-Type", "application/octet-stream"), r.status
        except urllib.error.HTTPError as e:
            body, ctype, status = e.read(), e.headers.get("Content-Type", "text/plain"), e.code
        except (OSError, http.client.HTTPException) as e:
            # Vraie panne en amont (délai, transfert coupé…) : la relayer telle
            # quelle — connexion fermée sans réponse — comme QGIS la verrait.
            print(f"UPSTREAM-FAIL {type(e).__name__} {request} "
                  f"STARTINDEX={start_index} COUNT={count}", flush=True)
            self.close_connection = True
            return
        if request == "getcapabilities":
            body = body.replace(b"https://data.geopf.fr/wfs", LOCAL.encode() + b"/wfs")
        print(f"PASS {status} {len(body)} o REQUEST={query.get('REQUEST')} "
              f"STARTINDEX={start_index} COUNT={count}", flush=True)
        try:
            self._send(status, ctype, body)
        except OSError:
            # QGIS a abandonné (délai réseau) avant que l'amont ne réponde.
            print(f"CLIENT-GONE {request} STARTINDEX={start_index}", flush=True)
            self.close_connection = True

    def _send(self, status, ctype, body, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"mandataire sur {LOCAL}, pannes={FAULTS}, silences={HANGS}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), _Handler).serve_forever()
