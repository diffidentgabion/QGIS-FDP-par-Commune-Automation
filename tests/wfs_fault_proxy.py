# -*- coding: utf-8 -*-
"""
Mandataire local d'injection de pannes pour le WFS Géoplateforme.

Relaie toute requête vers https://data.geopf.fr telle quelle, sauf les
GetFeature dont le STARTINDEX figure dans FAULTS : pour ceux-là, les N
premières requêtes reçoivent le dump de métriques Prometheus/JMX capturé
(HTTP 200, text/plain) — exactement ce que la passerelle Géoplateforme
renvoie lors d'un mauvais routage — puis passent normalement. Les réponses
GetCapabilities sont réécrites pour que les requêtes suivantes de QGIS
(GetFeature, DescribeFeatureType) transitent aussi par ce mandataire.

Usage :  python wfs_fault_proxy.py PORT [STARTINDEX:N,STARTINDEX:N…]
Voir docs/WFS ROBUSTESSE.md et tests/test_wfs_loader.py.
"""
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
FAULTS = {}
for spec in (sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else []):
    start_index, n = spec.split(":")
    FAULTS[start_index] = int(n)

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
        is_getfeature = (
            query.get("REQUEST", "").lower() == "getfeature"
            and query.get("RESULTTYPE", "").lower() != "hits"
        )
        with _lock:
            inject = is_getfeature and FAULTS.get(start_index, 0) > 0
            if inject:
                FAULTS[start_index] -= 1
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
        if query.get("REQUEST", "").lower() == "getcapabilities":
            body = body.replace(b"https://data.geopf.fr/wfs", LOCAL.encode() + b"/wfs")
        print(f"PASS {status} {len(body)} o REQUEST={query.get('REQUEST')} STARTINDEX={start_index}", flush=True)
        self._send(status, ctype, body)

    def _send(self, status, ctype, body, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"mandataire sur {LOCAL}, pannes={FAULTS}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), _Handler).serve_forever()
