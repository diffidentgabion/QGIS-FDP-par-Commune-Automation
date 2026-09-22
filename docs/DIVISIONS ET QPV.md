# Divisions infra-communales — IRIS et quartiers prioritaires

*Ajoutées le 2026-09-09. Deux couches décochées par défaut, en haut de la
légende : « Quartiers prioritaires (QPV) » juste sous les zonages de protection,
et « IRIS (quartiers INSEE) » dans un sous-groupe « Divisions communales ».*

## 1. Ce que c'est

« Quartier » désigne en France trois choses sans rapport, publiées par trois
organismes. Aucun jeu de données ne les réunit — ces deux couches couvrent
l'essentiel du besoin.

| Couche | Source | Nature |
|---|---|---|
| IRIS | INSEE, via Géoplateforme (`STATISTICALUNITS.IRIS:contours_iris`) | découpage statistique |
| QPV 2024 | ANCT, via data.gouv (GPKG) | zonage de politique publique |

**IRIS.** Le découpage statistique de l'INSEE, et en pratique la seule source de
noms de quartiers couvrant toute la France. Villeurbanne : 48 IRIS (45 de type
`H` habitat, 3 de type `A` activité), nommés Charmettes, Doua, Stalingrad, La
Soie… En dessous de ~5 000 habitants, l'INSEE ne découpe pas la commune : Affoux
(69001) renvoie un IRIS unique `690010000` de type `Z`, qui redouble la limite
communale déjà chargée. D'où la case décochée par défaut.

Les **grands quartiers** de l'INSEE, un cran au-dessus, ne demandent aucune
source supplémentaire : ils correspondent aux deux premiers chiffres du code
IRIS dans la commune (18 pour Villeurbanne). Non implémenté, mais c'est la
première chose que le sous-groupe « Divisions communales » est prévu pour
accueillir, avec les arrondissements municipaux et les communes déléguées
(`ADMINEXPRESS-COG-CARTO.LATEST:arrondissement_municipal` et
`:commune_associee_ou_deleguee`).

**QPV.** Les quartiers prioritaires de la politique de la ville, génération
2024 (décret n° 2023-1314). 1 362 périmètres en France hexagonale, ~1 300
communes concernées — la couche n'est donc créée pour presque aucune commune, et
c'est normal. Villeurbanne : 5 quartiers, de 9,5 à 47,7 ha.

## 2. Où c'est branché

| Fichier | Rôle |
|---|---|
| `fdp_par_commune.py` | entrées `_LAYER_CATALOGUE` (`qpv`, `iris`), `_DEFAULT_STYLES`, `_LABEL_FIELDS`, `_DIVISIONS_KEYS`, `_resolve_qpv_url`, `_qpv_zip_path`, `_load_qpv_layer`, `_apply_field_labels`, étape **4e-ter** de `_run_commune_import` |
| `fdp_ajout_couches.py` | `_divisions_group` + bloc QPV dédié (comme SIRENE et les arbres : pas de typename, donc absent de `wfs_entries`) |
| `tests/test_age_divisions.py` | T5 à T7, T9 |

`fdp_import_lot.py` n'a rien de spécifique : l'archive QPV est mise en cache sur
l'instance de l'algorithme, donc téléchargée une seule fois pour tout le lot.

## 3. Place dans la légende

L'ordre du catalogue est haut → bas dans la légende. Les deux entrées sont les
premières de la section « Fond de plan », donc immédiatement sous le sous-groupe
« Zonages de protection », qui est inséré en tête de la liste d'ordre.

```
Zonages de protection/        (sous-groupe, si coché)
Quartiers prioritaires (QPV)
Divisions communales/         (sous-groupe)
    IRIS (quartiers INSEE)
Établissements SIRENE
Commune
…
```

Les QPV restent **hors** du sous-groupe « Divisions communales » : c'est un
zonage de politique publique, pas une division administrative. Ils sont de la
même famille que les zonages de protection — périmètre réglementaire rendu
par-dessus le fond de plan — d'où leur écriture identique : remplissage quasi
transparent, contour tireté, teinte propre (`#B04A7A`).

L'IRIS, lui, est un découpage de lecture : contour tireté `#7A88A0` **sans
remplissage**, pour rester lisible par-dessus le bâti et les parcelles.

Les deux couches sont **étiquetées** (`nom_iris`, `lib_qp`), halo blanc, 7 pt —
seules couches étiquetées du projet. Sur un découpage infra-communal, le nom du
quartier *est* l'information ; aller le lire dans la table attributaire n'aurait
pas de sens. `_LABEL_FIELDS` mappe typename → champ et `_post_load_extras`
applique l'étiquetage dans le chargeur partagé, quel que soit le chemin pris.

## 4. Chargement

**IRIS** passe par `_load_code_insee_layer` — l'ancien `_load_parcelle_layer`,
généralisé. Filtre attributaire `CQL_FILTER code_insee='INSEE'` au lieu de
bbox + `native:clip` : un IRIS ne franchit jamais une limite communale, le
résultat est donc identique en évitant la sur-collecte et le découpage. Le repli
automatique sur bbox + découpage couvre Paris/Lyon/Marseille, dont les IRIS sont
tagués par arrondissement et non par commune-mère.

**QPV** n'a pas de service WFS — SIG Ville ne publie ni WFS, ni WMS, ni export
groupé, seulement des atlas PDF et un service de géocodage d'adresse. On prend
donc l'archive GPKG nationale sur data.gouv : 6 Mo, une seule fois par
exécution, puis filtre `insee_com = '<code>'` sur le fichier local (mesuré :
0,9 s). L'URL de la ressource est horodatée et change à chaque réédition : elle
est résolue par l'API data.gouv, avec `_QPV_ZIP_FALLBACK` en secours.

L'archive contient un fichier par territoire, chacun dans sa projection :
`QP2024_France_hexagonale_LB93.gpkg` est déjà en Lambert 93 (aucune
reprojection), l'outre-mer est pris dans le fichier WGS84 et transformé.

**Pas de découpage sur le contour communal** pour les QPV : un quartier
prioritaire peut déborder sur la commune voisine, et le tronquer masquerait
justement ce que l'on cherche à lire.

Une commune sans QPV ne reçoit **pas** de couche vide — `_load_qpv_layer`
renvoie `None`, comme `_load_osm_trees` sur une commune sans arbre.

## 5. Pièges désamorcés

**Disjoncteur QPV.** Comme pour Overpass et la BDNB, un échec de téléchargement
arme `self._qpv_down` et les communes suivantes du lot ne rappellent pas le
réseau.

**Réseau sur le thread graphique.** `_qpv_zip_path` passe par `_http_get`
(`QgsBlockingNetworkRequest`) et non par `requests.get` : voir `ARBRES OSM.md`
§5.

**Sous-groupe dédoublé.** À l'ajout sur une commune déjà importée,
`_divisions_group` cherche le sous-groupe « Divisions communales » existant
avant d'en créer un — sinon un deuxième groupe du même nom apparaîtrait à chaque
passage.

## 6. Tests

`python-qgis-ltr.bat tests/test_age_divisions.py T5 T6 T7 T9`

T5 charge les vrais QPV de Villeurbanne (5 quartiers, EPSG:2154, étiquettes
actives), T6 vérifie le `None` sur une commune sans QPV, T7 verrouille l'ordre
de légende et les styles, T9 charge les 48 IRIS de Villeurbanne par le filtre
`code_insee`.

## 7. Pistes non retenues

| Source | Pourquoi non |
|---|---|
| NPNRU / ANRU | publications régionales éparses, aucun service national |
| SIG Ville | atlas PDF et géocodage d'adresse, ni WFS ni export groupé |
| OSM `place=suburb` | 21 quartiers à Villeurbanne, mais 16 nœuds pour 5 polygones — des étiquettes, pas un découpage |
| Zonage PLU (`wfs_du:zone_urba`) | très utile, mais ce n'est pas un quartier : à traiter pour lui-même |
