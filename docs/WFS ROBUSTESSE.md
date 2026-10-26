# Robustesse du chargement WFS — réponses corrompues et silences de Géoplateforme

*Diagnostic et correctif du 2026-09-04, à la suite d'un import du Mans où
« la moitié des bâtiments » manquait.*

## 1. Symptôme

Import d'une grande commune (Le Mans : ~93 000 bâtiments dans l'emprise) :
la couche Bâti — et toutes ses couches dérivées (classification, SIRENE,
zones d'activité…) — est amputée d'un bloc géographique entier. Aucune erreur
dans le journal du traitement. Les petites communes n'étaient jamais touchées.

## 2. Cause : la passerelle data.geopf.fr

Rien n'a changé côté QGIS (3.40.15 LTR depuis février) ni côté scripts. Le
service WFS lui-même pagine correctement (pages de 5 000 — `CountDefault` —,
`STARTINDEX` profond OK, identifiants uniques et ordre stable).

En revanche la passerelle renvoie **sporadiquement, pour ~1 % des requêtes
GetFeature prises au hasard, une réponse HTTP 200 `text/plain` contenant un
dump de métriques Prometheus/JMX** (`jmx_exporter_build_info`,
`jvm_threads_current`…) à la place de la page GML. Non annoncé par l'IGN.
Échantillon dans `tests/fixtures/geopf_misroute_prometheus.txt`.

## 3. Pourquoi QGIS tronque en silence

Le fournisseur WFS de QGIS (vérifié dans les sources de la 3.40 et reproduit
hors interface) :

1. reçoit la page corrompue → journal : *« Erreur lors de l'analyse de la
   réponse GetFeature : not well-formed (invalid token) sur la ligne 1 »* ;
2. « rejoue » la requête 3 fois **immédiatement** — mais la passerelle (Kong)
   a livré la page corrompue avec `cache-control: private, max-age=1814400`
   (**21 jours**) : le cache réseau disque de QGIS
   (`%LOCALAPPDATA%\QGIS\QGIS3\cache`) la conserve et la ressert à chaque
   relance sans toucher le réseau (vérifié au mandataire local : une page
   injectée, aucune requête supplémentaire). Un seul mauvais routage suffit
   donc à faire échouer le téléchargement — et une couche recréée, ou le
   **prochain import de la même commune**, relit la même entrée tant qu'elle
   n'est pas évincée. L'entrée de l'import du Mans de 12 h 03 (page 25000 du
   bâti) a été retrouvée dans ce cache ;
3. journalise *« Le téléchargement des entités de la couche … a échoué ou
   partiellement échoué »* et **termine l'itérateur sans exception** sur les
   pages déjà reçues ;
4. `native:clip` découpe ce qu'il a reçu ; le script n'y voit rien.

Les pages suivent l'ordre des identifiants, groupés géographiquement : le
manque forme un bloc contigu (« la moitié de la ville »). Avec 19 pages pour
le bâti du Mans, ~17 % des imports étaient touchés — puis chaque nouvel essai
échouait au même endroit ; une commune tenant en une page n'est atteinte
qu'une fois sur cent — d'où « ça a toujours marché ».

Autres détails utiles découverts en chemin :

- QGIS **ignore la valeur `BBOX=`** de notre URI (forme URL complète) : il note
  seulement qu'une restriction spatiale est demandée. L'emprise réellement
  téléchargée est le `filterRect` de la requête — celui que `native:clip`
  passe (bbox du polygone communal). Comportement inchangé, mais à savoir.
- Le serveur renvoie ~2 % d'entités qui n'intersectent pas l'emprise ; QGIS
  les filtre. Un comptage `RESULTTYPE=hits` ne peut donc pas servir de
  contrôle d'intégrité exact.
- `provider.errors()` n'est renseigné qu'après un passage de la boucle
  d'événements (signal différé du thread de téléchargement).

## 4. Correctif (`fdp_par_commune.py`)

`_wfs_open` / `_wfs_download`, utilisés par `_load_wfs_layer` (donc par les
trois outils d'import) et par la voie rapide des parcelles :

1. créer la couche WFS et **télécharger immédiatement l'emprise complète**
   (`getFeatures(filterRect=bbox)` — la requête exacte que `native:clip`
   émettra ensuite, servie alors depuis le cache du fournisseur, sans coût
   réseau supplémentaire) ;
2. dépiler la boucle d'événements puis lire **`provider.errors()`** — signal
   d'échec indépendant de la langue de QGIS (le journal est en français dans
   l'interface) ;
3. en cas d'échec : **vidage du cache réseau de QGIS** (un retrait ciblé
   `cache.remove(QUrl)` s'est révélé non fiable — la clé ne correspond pas
   toujours à l'URL journalisée, p. ex. les apostrophes du filtre CQL des
   parcelles ; le cache ne contient que des tuiles et des pages WFS, et
   l'échec est rare), avertissement dans le journal du traitement, attente
   (2 s, 5 s, 10 s), puis **nouvelle couche** : la page est alors vraiment
   redemandée au serveur, avec ~1 % de risque de retomber sur un mauvais
   routage ;
4. après 4 tentatives : **exception explicite** (« Géoplateforme WFS en échec
   … Relancer l'import »). Jamais de couche partielle. En import en lot, la
   commune est signalée et ignorée ; en ajout de couches, la couche l'est.
   Même issue, depuis le 2026-10-23, quand la couche ne s'ouvre pas parce que
   le serveur est muet (§7).

Voie rapide des parcelles (filtre `code_insee`) : même contrôle et même
vidage, un seul nouvel essai, puis repli sur bbox + découpage (parade
complète) plutôt qu'une longue attente.

Le journal affiche désormais `✓ N entité(s) téléchargée(s)` par couche : le
compte doit être stable d'un import à l'autre pour la même commune.

L'ancien contrôle `featureCount() == 0` a été retiré : sur ce fournisseur il
renvoyait le compte **national** (51 M pour le bâti), donc ne détectait
jamais rien et ajoutait un aller-retour exposé au même défaut.

## 5. Vérification

`tests/wfs_fault_proxy.py` : mandataire local qui relaie vers data.geopf.fr et
injecte la réponse Prometheus pour des `STARTINDEX` choisis, N fois — ou,
depuis le §7, un silence (`hang@caps`, `hang@describe`, `hang@probe`,
`hang@STARTINDEX`).
`tests/test_wfs_loader.py` : exécute le vrai `_load_wfs_layer` /
`_load_parcelle_layer` sur Le Mans, serveur réel puis pannes injectées
(page centrale corrompue une fois / à jamais, première page corrompue,
parcelles, couche vide, typename inexistant).

```
python-qgis-ltr.bat tests/test_wfs_loader.py          # tous
python-qgis-ltr.bat tests/test_wfs_loader.py T2 T3    # sélection
```

Résultats du 2026-09-04 : voir §6.

## 6. Résultats de la campagne du 2026-09-04

Le Mans (72181), QGIS 3.40.15 LTR, code final :

| Test | Résultat | Avert. | Mandataire |
|---|---|---|---|
| T1 bâti, serveur réel | 89 218 téléchargées → 73 424 découpées | 0 | — |
| T2 bâti, page 10000 corrompue une fois | 73 424 (identique à T1) | 1 | 1 injectée, la relance a bien redemandé la page |
| T3 bâti, page 10000 corrompue à jamais | **exception** après 4 tentatives | 3 | 4 injectées = 4 vraies requêtes réseau |
| T4 bâti, première page corrompue | 73 424 | 0 | 1 injectée, récupérée par QGIS lui-même |
| T5 parcelles, serveur réel | 55 479 (filtre code_insee) | 0 | — |
| T6 parcelles, page 5000 corrompue une fois | 55 479, sans repli | 1 | 1 injectée, la relance a bien redemandé la page |
| T7 piste d'aérodrome (4 entités) | 4, pas d'exception | 0 | — |
| T8 typename inexistant | None + « couche invalide » | 4 | — |

Sans vidage du cache (version intermédiaire), T6 échouait à la relance et
partait en repli : c'est le test qui a révélé le rôle du cache. Pendant la
campagne, une panne **réelle** a aussi été observée sur les parcelles (4 échecs
consécutifs de la même URL) — correctement récupérée par le repli.

Avant correctif, 9 téléchargements de contrôle du bâti du Mans avaient rendu
91 536 entités chacun (emprise géo API légèrement différente de T1) : le
défaut ne se manifeste que lorsqu'une page tombe sur le mauvais routage, puis
persiste via le cache.

## 7. Serveur muet (délais dépassés) — 2026-10-23

### Symptôme

Journal QGIS (onglet WFS) : des rafales de *« Le téléchargement des entités
à échoué : L'opération est annulée »*, *« Relance de la requête … 1/3 »*
espacées d'exactement 60 s, et *« Échec de GetCapabilities pour l'URL … :
L'opération est annulée »*. Toutes les sortes de requêtes sont touchées.

### Cause

`data.geopf.fr` laisse parfois une requête sans **un seul octet** de réponse.
Mesuré le 2026-10-23 : un GetCapabilities sur trois (5,5 Mo, servi
d'ordinaire en 0,6 s) muet pendant 120 s ; pendant les tests, aussi un HTTP
500 sur GetCapabilities et des transferts coupés en cours de page. QGIS
abandonne au bout de son délai réseau (60 s par défaut, Options › Réseau —
réglage de l'utilisateur, que les scripts ne modifient pas) : c'est
« L'opération est annulée ».

### Effet selon la requête (vérifié au mandataire, `hang@…`)

| Requête muette | Réaction de QGIS | Effet sur l'import |
|---|---|---|
| GetCapabilities | aucune relance : couche invalide | **avant correctif : couche ignorée en silence** |
| DescribeFeatureType | aucune relance : couche invalide | **idem** |
| GetFeature `COUNT=1` (vérification à la création de la couche, sans BBOX) | 3 relances (4 min), puis continue | aucun : couche valide, même type, même compte (T11) |
| page GetFeature | relance ; si les 4 essais échouent → `provider.errors()` | aucun : parade du §4 |

Le cas grave : `_wfs_open` traitait toute couche invalide comme un typename
inexistant (T8) — 4 essais, puis `None` et un simple avertissement
« couche invalide ». L'import continuait **sans la couche et tout ce qui en
dérive** (pour le bâti : placement SIRENE, catégories d'activité, analyse du
bâti) et l'import en lot comptait la commune comme réussie. Exactement la
troncature silencieuse que le §4 devait empêcher, par une autre voie.

### Correctif (`fdp_par_commune.py`)

À la première couche invalide, `_wfs_typename_unknown` pose la question au
serveur : un DescribeFeatureType direct (`_http_get`, sans cache, délai 30 s,
annulable). À un typename inconnu Géoplateforme répond **HTTP 400**
(`ExceptionReport` « Could not find type ») : la couche est ignorée tout de
suite, avec un avertissement qui nomme le typename. Toute autre issue (schéma,
délai dépassé, 5xx…) est une panne passagère : les essais continuent, puis
**exception** « Géoplateforme WFS en échec après 4 tentatives — couche
invalide. Relancer l'import. » La question n'est posée qu'une fois : muet, le
serveur coûterait 30 s de plus à chaque essai.

Effet de bord : T8 rend désormais la main en ~1 s avec un seul avertissement,
au lieu de 18 s et quatre.

Parcelles et IRIS (voie rapide `code_insee`) : une couche invalide donne
n=0, donc le repli bbox + découpage (« filtre code_insee inutilisable
(n=0) » — libellé trompeur dans ce cas, mais comportement correct), qui passe
par `_wfs_open` et bénéficie du correctif.

### Coût en temps

Chaque silence coûte 60 s ; la vérification `COUNT=1` jusqu'à 4 min (les
entrées `batiment … COUNT=1` du journal). Un serveur durablement muet : environ
4 à 5 min par couche, puis exception.

### Artefact de journal à ignorer

`…cql_filter=code_insee3D%2775114%27…` dans *« Relance de la requête »* (le
`%` de `%3D` a disparu) n'est pas une requête malformée : QGIS compose ce
message par `QString::arg()` enchaînés et le troisième argument (le maximum
de relances, 3) remplace le `%3` de l'URL. La ligne GetCapabilities montre la
vraie URL, avec `%3D`.

### Résultats du 2026-10-23

Le Mans (72181), QGIS 3.40.15 LTR, silences de 75 s injectés. Les tests au
mandataire relaient le reste vers le vrai serveur, lui-même instable ce
jour-là (T1 a subi un vrai silence).

| Test | Avant correctif | Après correctif | Mandataire |
|---|---|---|---|
| T1 bâti, serveur réel | 73 424, 1 avert. (vrai silence récupéré) | — | — |
| T8 typename inexistant | None, 4 avert., 18 s | None, 1 avert., 1,1 s | — |
| T9 GetCapabilities muet une fois | 73 424, 1 avert. | (inchangé) | 1 silence |
| T10 GetCapabilities muet à jamais | **None**, 4 avert., 257 s | **exception**, 3 avert., 258 s | 4 silences ; vérification DescribeFeatureType → 200 |
| T11 vérif. `COUNT=1` muette à jamais | 73 424, MultiPolygon, 0 avert., 358 s | (inchangé) | 4 silences |
| T12 première page muette une fois | 73 424, 0 avert. | (inchangé) | 1 silence |
| T13 DescribeFeatureType muet à jamais | **None**, 4 avert., 263 s | **exception**, 3 avert., 291 s | 4 silences + 1 (la vérification, 30 s) |

## 8. Filtre attributaire qui ne retient rien : 4 min perdues — 2026-10-23

### Symptôme

Import d'un arrondissement de Paris : le journal QGIS aligne, pour les
parcelles, quatre *« Relance de la requête …cql_filter=code_insee='75113'…
STARTINDEX=0&COUNT=1 »* à une minute d'intervalle, à chaque import et pour
chaque arrondissement. Ce n'est pas une panne passagère (§7) : c'est
reproductible à 100 %.

### Cause

1. Le Parcellaire Express n'étiquette pas les parcelles des arrondissements
   PLM par leur code d'arrondissement : une parcelle de Paris 13e porte
   `code_insee='75056'`, `code_arr='113'` (seul `idu` commence par `75113`).
   `code_insee='75113'` ne retient donc **aucune** parcelle. Les IRIS font
   l'inverse : `75113` → 73 IRIS, `75056` → 0.
2. À la création de la couche, le fournisseur WFS de QGIS lit une première
   entité (`STARTINDEX=0&COUNT=1`). Avec un filtre qui ne retient rien,
   Géoplateforme parcourt toute la table dans l'ordre des identifiants sans
   rien trouver : **HTTP 504 au bout de 60 s** (délai de la passerelle), et
   QGIS, au même délai, annule puis relance trois fois. Le comptage
   (`RESULTTYPE=hits`) du même filtre répond en 0,3 s ; un filtre qui retient
   des entités, en 0,3–0,5 s ; une emprise BBOX vide aussi (index spatial).
3. Le script voyait ensuite `featureCount() == 0` et se repliait sur
   bbox + découpage — mais 4 min plus tard, à chaque arrondissement.

### Correctif (`fdp_par_commune.py`)

- `_wfs_hits` : comptage direct (`_http_get`, 30 s) **avant** de créer la
  couche QGIS. Filtre vide ou invraisemblable → repli immédiat ; comptage
  impossible (serveur muet) → repli aussi, la voie bbox ayant toutes les
  parades des §4 et §7.
- `_code_insee_filter` / `_PLM_ARRONDISSEMENTS` : parcelles d'un
  arrondissement PLM filtrées par `code_insee='<commune parente>' AND
  code_arr='<code sans département>'` — voie rapide exacte au lieu du repli.
  L'entité témoin est vérifiée sur tous les attributs du filtre.

| Test (serveur réel) | Avant | Après |
|---|---|---|
| T14 parcelles Paris 13e | 4 min de relances, puis repli bbox | 4 641 par filtre, 5,7 s |
| T15 IRIS Paris entier | 4 min de relances, puis repli bbox | repli immédiat, 4,4 s |
| T5 parcelles Le Mans | 55 479 | 55 479 (inchangé) |
| T6 parcelles, page corrompue | 55 479, 1 avert. | 55 479, 1 avert. (inchangé) |
