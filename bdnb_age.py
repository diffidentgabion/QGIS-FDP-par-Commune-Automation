# -*- coding: utf-8 -*-
"""
bdnb_age.py — Année de construction du bâti (BDNB, CSTB)

La BD TOPO ne publie pas d'année de construction. Son champ
`date_d_apparition` en tient lieu, mais il n'est renseigné que sur une partie
du bâti (mesuré : ~70 % sur un échantillon lyonnais, ~36 % en rural). La Base
de Données Nationale des Bâtiments (BDNB) publie la même information, tirée des
fichiers fonciers, avec une bien meilleure couverture (mesuré : 305 548 /
319 159 groupes de bâtiments renseignés dans le 69, soit 96 %) et surtout une
clé de jointure EXACTE vers la BD TOPO — pas d'appariement géométrique.

Deux fichiers de l'export départemental suffisent :
  • ``batiment_groupe_ffo_bat.csv``        → ``annee_construction`` par groupe
  • ``rel_batiment_groupe_bdtopo_bat.csv`` → ``bdtopo_bat_cleabs`` → groupe

L'export départemental complet pèse ~550 Mo, mais on n'a besoin que de ces deux
membres (~21 Mo compressés). On lit donc le répertoire central du ZIP par
requêtes HTTP Range, puis on télécharge et décompresse les deux membres voulus.

Pourquoi pas ``/vsizip//vsicurl/`` (GDAL sait le faire nativement) : le
téléchargement se ferait dans du code C non interruptible, sur le thread
graphique (tous les algorithmes du dépôt sont FlagNoThreading), donc fenêtre
QGIS figée et bouton Annuler inopérant — exactement la panne documentée dans
docs/ARBRES OSM.md §5. Ici, chaque aller-retour passe par le `http_get` fourni
par l'appelant, qui s'appuie sur QgsBlockingNetworkRequest.

L'API ouverte api.bdnb.io n'est pas utilisable pour cet usage : elle plafonne à
10 entités par appel (en-tête ``X-Max-Items-Per-Call``) et 10 000 appels par
mois — une seule commune moyenne épuiserait une fraction notable du quota.
"""

import csv
import io
import re
import struct
import zlib

# Millésime utilisé si data.gouv est injoignable. Le nom du millésime figure
# dans le chemin S3 des exports ; il change environ deux fois par an.
MILLESIME_FALLBACK = "2026-02-a"

_DATAGOUV_API = (
    "https://www.data.gouv.fr/api/1/datasets/"
    "base-de-donnees-nationale-des-batiments/"
)
_S3_BASE = "https://open-data.s3.fr-par.scw.cloud"

_FFO_MEMBER = "csv/batiment_groupe_ffo_bat.csv"
_REL_MEMBER = "csv/rel_batiment_groupe_bdtopo_bat.csv"

# Fin de fichier lue pour trouver l'« End Of Central Directory ». 64 Kio
# couvrent le commentaire ZIP maximal (65 535 octets) + l'enregistrement.
_EOCD_TAIL = 65_600
_EOCD_SIG = b"PK\x05\x06"
_CD_SIG = b"PK\x01\x02"

# Bornes de vraisemblance : la BDNB descend jusqu'à 1200 sur des bâtiments
# médiévaux réels ; au-delà de ces bornes, la valeur est une scorie de saisie.
_ANNEE_MIN = 1000
_ANNEE_MAX = 2100

# Cache de session : (millésime, dep) → {nom de membre: octets COMPRESSÉS}.
# On garde le compressé (~21 Mo) et non le décompressé (~71 Mo) : la
# décompression coûte ~0,3 s, largement moins qu'un nouveau téléchargement.
# Borné à deux départements pour ne pas laisser filer la mémoire sur un lot
# qui traverserait la France.
_MEMBER_CACHE = {}
_CACHE_MAX_DEPS = 2

_millesime_cache = None


class BdnbError(Exception):
    """Panne côté BDNB — l'appelant se rabat sur date_d_apparition."""


def _range_get(url, start, end, http_get, feedback, label):
    """Télécharge les octets [start, end] (bornes incluses) d'une URL."""
    status, content, panne = http_get(
        url, feedback, headers={"Range": f"bytes={start}-{end}"}
    )
    if panne is not None:
        raise BdnbError(f"{label} : {panne}")
    # Un serveur qui ignore l'en-tête Range répond 200 avec le fichier ENTIER
    # (~550 Mo ici). On refuse plutôt que de saturer la mémoire.
    if status != 206:
        raise BdnbError(f"{label} : Range non honoré (HTTP {status})")
    return content


def resolve_millesime(http_get, feedback):
    """
    Millésime courant, lu sur data.gouv. Les URL des exports nationaux
    contiennent le millésime (…/bdnb_millesime_2026-02-a/…) ; les exports
    départementaux, non publiés sur data.gouv, suivent le même chemin S3.
    Renvoie MILLESIME_FALLBACK si l'API est injoignable.
    """
    global _millesime_cache
    if _millesime_cache is not None:
        return _millesime_cache
    try:
        status, content, panne = http_get(_DATAGOUV_API, feedback)
        if panne is not None:
            raise BdnbError(panne)
        found = re.search(
            rb"bdnb_millesime_(\d{4}-\d{2}-[a-z])", content
        )
        if not found:
            raise BdnbError("millésime absent de la réponse data.gouv")
        _millesime_cache = found.group(1).decode("ascii")
    except Exception as exc:  # data.gouv en panne : le millésime figé fait foi
        if feedback is not None:
            feedback.pushInfo(
                f"   ℹ  BDNB : millésime non résolu ({exc}) — "
                f"repli sur {MILLESIME_FALLBACK}."
            )
        _millesime_cache = MILLESIME_FALLBACK
    return _millesime_cache


def dep_zip_url(millesime, dep):
    """URL de l'export CSV départemental (les fichiers OM suivent le même nom)."""
    return (
        f"{_S3_BASE}/bdnb_millesime_{millesime}"
        f"/millesime_{millesime}_dep{dep}"
        f"/open_data_millesime_{millesime}_dep{dep}_csv.zip"
    )


def _central_directory(url, http_get, feedback):
    """
    {nom de membre: (offset d'entête locale, taille compressée, méthode)}.

    Deux allers-retours : la queue du fichier (pour l'EOCD, qui donne la
    position du répertoire central), puis le répertoire central lui-même
    (~9 Ko pour les 119 membres d'un export départemental).
    """
    # Range suffixe : « les N derniers octets », sans connaître la taille.
    status, tail, panne = http_get(
        url, feedback, headers={"Range": f"bytes=-{_EOCD_TAIL}"}
    )
    if panne is not None:
        raise BdnbError(f"queue du ZIP : {panne}")
    if status != 206:
        raise BdnbError(f"queue du ZIP : Range non honoré (HTTP {status})")

    pos = tail.rfind(_EOCD_SIG)
    if pos < 0:
        raise BdnbError("archive illisible (EOCD introuvable)")
    cd_size, cd_offset = struct.unpack("<II", tail[pos + 12 : pos + 20])
    # 0xFFFFFFFF = archive ZIP64 (> 4 Gio). Les exports départementaux font
    # ~550 Mo ; si le format change un jour, mieux vaut un échec explicite
    # (repli sur date_d_apparition) qu'une lecture d'offsets fantaisistes.
    if 0xFFFFFFFF in (cd_size, cd_offset):
        raise BdnbError("archive ZIP64 non gérée")

    raw = _range_get(
        url, cd_offset, cd_offset + cd_size - 1, http_get, feedback,
        "répertoire central",
    )
    members = {}
    at = 0
    while at < len(raw) and raw[at : at + 4] == _CD_SIG:
        method = struct.unpack("<H", raw[at + 10 : at + 12])[0]
        csize = struct.unpack("<I", raw[at + 20 : at + 24])[0]
        n_len, e_len, c_len = struct.unpack("<HHH", raw[at + 28 : at + 34])
        local_offset = struct.unpack("<I", raw[at + 42 : at + 46])[0]
        name = raw[at + 46 : at + 46 + n_len].decode("utf-8", "replace")
        members[name] = (local_offset, csize, method)
        at += 46 + n_len + e_len + c_len
    if not members:
        raise BdnbError("répertoire central vide")
    return members


def _fetch_member(url, member, entry, http_get, feedback):
    """Octets COMPRESSÉS d'un membre du ZIP (deux allers-retours Range)."""
    local_offset, csize, method = entry
    if method not in (0, 8):
        raise BdnbError(f"{member} : compression {method} non gérée")
    # L'entête locale répète le nom mais avec des longueurs qui lui sont
    # propres : il faut la lire pour connaître le début réel des données.
    header = _range_get(
        url, local_offset, local_offset + 29, http_get, feedback, member
    )
    n_len, e_len = struct.unpack("<HH", header[26:30])
    start = local_offset + 30 + n_len + e_len
    return _range_get(
        url, start, start + csize - 1, http_get, feedback, member
    ), method


def _members_bytes(dep, millesime, http_get, feedback):
    """Les deux membres utiles, compressés, avec cache de session."""
    key = (millesime, dep)
    if key in _MEMBER_CACHE:
        return _MEMBER_CACHE[key]

    url = dep_zip_url(millesime, dep)
    directory = _central_directory(url, http_get, feedback)
    wanted = {}
    for member in (_REL_MEMBER, _FFO_MEMBER):
        if member not in directory:
            raise BdnbError(f"{member} absent de l'archive")
        wanted[member] = _fetch_member(
            url, member, directory[member], http_get, feedback
        )

    if len(_MEMBER_CACHE) >= _CACHE_MAX_DEPS:
        _MEMBER_CACHE.clear()
    _MEMBER_CACHE[key] = wanted
    return wanted


def _rows(compressed):
    """Lignes d'un membre CSV BDNB (séparateur « ; », entête en première ligne)."""
    raw, method = compressed
    data = zlib.decompress(raw, -15) if method == 8 else raw
    reader = csv.reader(
        io.StringIO(data.decode("utf-8", "replace")), delimiter=";"
    )
    header = next(reader, None)
    if not header:
        raise BdnbError("membre CSV vide")
    return header, reader


def annees_construction(dep, cleabs, http_get, feedback, millesime=None):
    """
    {cleabs BD TOPO: année de construction} pour les seuls `cleabs` demandés.

    `http_get(url, feedback, headers=None)` doit renvoyer (statut, octets,
    panne) — voir FDPParCommune._http_get. Lève BdnbError si la BDNB n'a rien
    livré d'exploitable ; l'appelant se rabat alors sur date_d_apparition.

    Le filtrage par `cleabs` dès la première passe garde la mémoire
    proportionnelle à la commune et non au département (583 887 cleabs pour le
    seul 69).
    """
    wanted = {c for c in cleabs if c}
    if not wanted:
        return {}
    millesime = millesime or resolve_millesime(http_get, feedback)
    members = _members_bytes(dep, millesime, http_get, feedback)

    # Passe 1 — relation BD TOPO → groupe BDNB, restreinte à nos bâtiments.
    header, reader = _rows(members[_REL_MEMBER])
    try:
        i_cleabs = header.index("bdtopo_bat_cleabs")
        i_groupe = header.index("batiment_groupe_id")
    except ValueError:
        raise BdnbError("colonnes inattendues dans la table de relation")
    cleabs_to_groupe = {}
    for row in reader:
        if len(row) > i_cleabs and row[i_cleabs] in wanted:
            cleabs_to_groupe[row[i_cleabs]] = row[i_groupe]
    if not cleabs_to_groupe:
        return {}

    # Passe 2 — année de construction des groupes retenus.
    besoin = set(cleabs_to_groupe.values())
    header, reader = _rows(members[_FFO_MEMBER])
    try:
        i_groupe = header.index("batiment_groupe_id")
        i_annee = header.index("annee_construction")
    except ValueError:
        raise BdnbError("colonnes inattendues dans la table fichiers fonciers")
    groupe_to_annee = {}
    for row in reader:
        if len(row) <= i_annee or row[i_groupe] not in besoin:
            continue
        texte = row[i_annee].strip()
        if not texte.isdigit():
            continue  # colonne vide (bâtiment sans local foncier) ou illisible
        annee = int(texte)
        if _ANNEE_MIN <= annee <= _ANNEE_MAX:
            groupe_to_annee[row[i_groupe]] = annee

    return {
        cle: groupe_to_annee[groupe]
        for cle, groupe in cleabs_to_groupe.items()
        if groupe in groupe_to_annee
    }
