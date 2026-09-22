# Année de construction du bâti (BDNB)

*Ajoutée le 2026-09-09. Enrichit la couche « Bâti » existante — aucune couche
nouvelle : un clic sur un bâtiment affiche son année de construction dans la
fenêtre d'identification.*

## 1. Ce que c'est

Deux champs ajoutés à la couche « Bâti » au chargement :

| Champ | Alias affiché | Contenu |
|---|---|---|
| `annee_construction` | Année de construction | entier, ou NULL |
| `source_annee` | Source de l'année | `BDNB` ou `BD TOPO` |

`date_d_apparition` reçoit aussi un alias (« Année d'apparition (BD TOPO) ») :
malgré son nom, il ne s'agit pas de la date d'entrée de l'objet dans la base.

## 2. Pourquoi deux sources

La BD TOPO **ne publie pas** d'année de construction. Son champ
`date_d_apparition` en tient lieu — il vient de l'appariement aux fichiers
fonciers, d'où le champ voisin `appariement_fichiers_fonciers` — mais il n'est
renseigné que sur une partie du bâti. La **BDNB** (CSTB) publie la même
information foncière, beaucoup plus largement, et avec une clé de jointure
exacte vers la BD TOPO.

Mesures du 2026-09-08 :

| Source | Couverture |
|---|---|
| `date_d_apparition`, échantillon Lyon (500 bâtiments) | 70 % |
| `date_d_apparition`, échantillon rural (Affoux, 345 bâtiments) | 36 % |
| BDNB, groupes de bâtiments du département 69 | 305 548 / 319 159 = **96 %** |
| BDNB, après jointure sur l'échantillon rural | 46 % |

Deux chiffres à ne pas confondre : la BDNB renseigne 96 % de **ses** groupes de
bâtiments, mais elle ne couvre que le résidentiel et le tertiaire — granges,
hangars et annexes n'ont pas de local foncier, d'où les 46 % sur un échantillon
rural. Le même calcul en ville monte beaucoup plus haut.

Sur les bâtiments où les deux sources existent, elles concordent sur
**95 / 102**. L'union des deux couvre donc plus que chacune séparément : d'où le
repli systématique sur `date_d_apparition` plutôt qu'un choix de source unique.

Valeurs extrêmes réelles : 1200 à 2025, médiane 1977 pour le 69. `1800-01-01`
dans la BD TOPO et `1800` dans la BDNB sont une sentinelle « antérieur à », pas
une année relevée.

## 3. Où c'est branché

| Fichier | Rôle |
|---|---|
| `bdnb_age.py` | lecture du ZIP départemental par requêtes Range, jointure, cache de session |
| `fdp_par_commune.py` | `_http_get`, `_post_load_extras`, `_enrich_bati_annee`, `_annee_from_date` |
| `fdp_ajout_couches.py` | transmet `insee` à `_load_wfs_layer` — l'enrichissement suit |
| `tests/test_age_divisions.py` | T1 à T4 |

L'enrichissement est déclenché depuis `_post_load_extras`, appelé par les deux
chargeurs partagés (`_load_wfs_layer` et `_load_code_insee_layer`) : « FDP par
Commune », « Import en lot » et « Ajout aux communes existantes » en bénéficient
sans code propre. Le déclencheur est le typename `BDTOPO_V3:batiment` plus un
`insee` non nul — sans code commune, pas de département, donc pas de BDNB.

## 4. 21 Mo au lieu de 550

L'export départemental CSV de la BDNB pèse ~550 Mo (dep 69 : 546 360 810
octets), l'export France 39 Go. On n'a besoin que de deux membres :

| Membre | Compressé | Apport |
|---|---:|---|
| `csv/batiment_groupe_ffo_bat.csv` | 4,9 Mo | `annee_construction` par groupe |
| `csv/rel_batiment_groupe_bdtopo_bat.csv` | 16,2 Mo | `bdtopo_bat_cleabs` → groupe |

`bdnb_age` lit donc le **répertoire central du ZIP** par requêtes HTTP Range
(queue du fichier pour l'EOCD, puis le répertoire lui-même, ~9 Ko pour 119
membres), puis télécharge et décompresse les deux membres voulus. Mesuré :
6,3 s de réseau, 0,8 s de parsing.

La jointure se fait sur `cleabs`, l'identifiant BD TOPO déjà présent dans la
couche : **aucun appariement géométrique**. Mesuré sur un échantillon réel,
95 % des bâtiments BD TOPO trouvent leur groupe BDNB.

Les deux passes sont filtrées dès la première ligne par l'ensemble des `cleabs`
de la commune : la mémoire reste proportionnelle à la commune, pas au
département (583 887 `cleabs` pour le seul 69).

## 5. Pièges désamorcés

**Pas de `/vsizip//vsicurl/`.** GDAL sait lire un membre de ZIP distant — c'est
même ainsi que la faisabilité a été vérifiée (1,2 s à l'ouverture). Mais le
téléchargement se ferait alors dans du code C non interruptible, sur le thread
graphique, puisque tous les algorithmes du dépôt sont `FlagNoThreading` : QGIS
figé, bouton Annuler inopérant. C'est exactement la panne du 2026-09-08 décrite
dans `ARBRES OSM.md` §5. Tout passe donc par `_http_get`, bâti sur
`QgsBlockingNetworkRequest`.

**Range non honoré.** Un serveur qui ignore l'en-tête `Range` répond 200 avec le
fichier entier — 550 Mo en mémoire. `bdnb_age` exige un 206 et échoue sinon.

**ZIP64.** Au-delà de 4 Gio, les offsets du répertoire central valent
`0xFFFFFFFF` et renvoient ailleurs. Les exports départementaux font ~550 Mo,
mais le cas est détecté et provoque un échec explicite plutôt qu'une lecture
d'offsets fantaisistes (l'export France, lui, est bien en ZIP64).

**Millésime mouvant.** Le chemin S3 contient le millésime
(`bdnb_millesime_2026-02-a`), qui change ~2 fois par an. Il est lu sur l'API
data.gouv du jeu de données national, puis réutilisé pour construire l'URL
départementale — même technique que `_resolve_datagouv_parquet_url` pour
SIRENE. `MILLESIME_FALLBACK` sert si data.gouv est injoignable.

**Disjoncteur.** Une panne BDNB arme `self._bdnb_down` : les communes suivantes
du lot ne rappellent pas le réseau et se rabattent directement sur
`date_d_apparition`. Sans lui, un lot de vingt communes repaierait vingt fois
le coût de l'échec — la leçon d'Overpass.

**L'API ouverte n'est pas utilisable ici.** `api.bdnb.io` ne demande pas de clé,
mais plafonne à **10 entités par appel** (en-tête `X-Max-Items-Per-Call`) et
10 000 appels par mois. Une commune de 5 000 bâtiments consommerait 500 appels,
Lyon 60 % du quota mensuel. Elle reste pertinente pour consulter un seul
bâtiment, pas pour en dater une commune.

## 6. Tests

`python-qgis-ltr.bat tests/test_age_divisions.py T1 T2 T3 T4`

T2 interroge la vraie BDNB sur deux bâtiments témoins du 69 (`…951543` → 2020,
`…713342` → 1800) et vérifie que le second appel est servi par le cache. T3
injecte une panne via `_http_get` et vérifie le repli plus le disjoncteur.

## 7. Pistes non retenues

| Source | Pourquoi non |
|---|---|
| Fichiers fonciers (DGFiP/Cerema) | accès restreint ; la BDNB en est l'ouverture |
| API ADEME DPE | `annee_construction` seulement pour les logements ayant un DPE, et clé BAN à géocoder |
| OSM `start_date` | couverture négligeable en France |
| RNB | identifiants stables, aucune date |
| Cadastre Etalab / PCI | aucune date |
