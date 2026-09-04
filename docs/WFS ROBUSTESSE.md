# Robustesse du chargement WFS — réponses corrompues de Géoplateforme

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
injecte la réponse Prometheus pour des `STARTINDEX` choisis, N fois.
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
