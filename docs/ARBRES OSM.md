# Couche « Arbres (OSM) » — arbres isolés OpenStreetMap

*Ajoutée le 2026-09-08. Première source de données du projet qui ne vient ni de
la Géoplateforme ni de data.gouv. Section 5 : correctif du gel de QGIS constaté
le jour même sur Le Mans et ses voisines.*

## 1. Ce que c'est

Les arbres cartographiés un par un dans OpenStreetMap (`natural=tree`),
interrogés à la volée par l'**API Overpass** sur l'emprise, puis découpés sur le
contour communal — même séquence que `_load_wfs_layer`.

C'est la seule base d'arbres individuels disponible partout en France. Les
inventaires communaux (Paris, Lyon, Nantes…) sont plus riches mais ont chacun
leur portail et leur schéma ; la BD Forêt et `zone_de_vegetation` ne
fournissent que des polygones.

**Couverture très inégale — c'est contributif, pas exhaustif.** Mesures du
2026-09-08, après découpage sur le contour :

| Commune | Arbres | Remarque |
|---|---:|---|
| Le Mans (72181) | 2 574 | l'essentiel du secteur |
| Coulaines (72095) | 125 | |
| Autun (71014) | 184 | 432 dans sa bbox — le découpage en retire 57 % |
| Saint-Pavace (72310) | 0 | 157 dans sa bbox, tous chez le voisin |

Deux enseignements : le compte dans la **bbox** n'a rien à voir avec le compte
dans la **commune** (une petite commune limitrophe d'une ville en cadre surtout
la ville), et une commune sans aucun arbre est un cas courant — la couche n'est
alors pas créée du tout, un calque vide dans la légende n'apprenant rien.

Le découpage a été vérifié contre le filtre `area["ref:INSEE"]` d'Overpass, qui
compte côté serveur sans passer par notre code : identique sur 5 communes sur 6,
Le Mans à 10 arbres près (0,4 %) — l'écart entre les limites AdminExpress de
l'IGN, avec lesquelles nous découpons, et celles d'OSM.

## 2. Où c'est branché

| Fichier | Rôle |
|---|---|
| `fdp_par_commune.py` | entrée `_LAYER_CATALOGUE` (`style_key: "osm_arbres"`), `_overpass_request`, `_overpass_fetch`, `_osm_trees_query`, `_osm_trees_layer`, `_prefetch_osm_trees`, `_load_osm_trees`, `_apply_osm_arbres_style`, `_commune_bbox`, étape **4e-bis** de `_run_commune_import` |
| `fdp_ajout_couches.py` | pré-chargement groupé avant la boucle + bloc dédié après les couches WFS (comme SIRENE : pas de `typename`, donc absente de `wfs_entries`) |
| `fdp_import_lot.py` | pré-chargement groupé, à côté de celui de SIRENE |

Case à cocher **indépendante** de la couche « OpenStreetMap » : celle-ci est un
fond de tuiles raster pré-rendu, les arbres sont une requête vectorielle sur la
base OSM. On veut couramment l'un sans l'autre. Décochée par défaut : chaque
import ajouterait sinon un aller-retour Overpass.

## 3. Symbologie

Même langage que la Végétation haute (`_apply_bdforet_style`) : le tag OSM
`leaf_type` est l'équivalent ponctuel du poste `tfv_g11` de la BD Forêt, et la
palette de verts est **identique**.

| Règle | Filtre | Couleur |
|---|---|---|
| Feuillu | `leaf_type = 'broadleaved'` | `#8CCD8C` (= Forêt fermée de feuillus) |
| Conifère | `leaf_type = 'needleleaved'` | `#5FA878` (= Forêt fermée de conifères) |
| Essence non précisée | `ELSE` | `#74BC82` (= Forêt fermée mixte) |

Deux écarts assumés par rapport aux surfaces :

- **contour vert foncé `#4A8A5E`, 0,2 mm** — sans lui, un point `#8CCD8C` posé
  sur un polygone « Forêt fermée de feuillus » de la même couleur est invisible.
  Les remplissages, eux, n'ont aucun contour ;
- **repli en vert moyen, pas en vert pâle** — `leaf_type` manque sur la majorité
  des arbres : c'est le cas dominant, pas un résidu.

Pas de règle « mixte » : `leaf_type=mixed` n'a pas de sens sur un arbre unique,
la règle n'apparaîtrait dans la légende que pour ne jamais rien sélectionner.

## 4. Choix des serveurs

`_OVERPASS_URLS` ne contient que les deux instances publiques généralistes,
`overpass-api.de` et `overpass.kumi.systems`. Elles n'accordent que **deux
créneaux simultanés par adresse IP** et répondent HTTP 504 dès qu'elles saturent.

Deux autres ont été essayées puis **écartées — ne pas les rajouter** :

- **`overpass.osm.ch`** — répond 200 en 0,5 s… avec **0 arbre partout en
  France**. C'est un extrait régional suisse. Une réponse vide *et valide* est
  le pire des cas : ni erreur, ni donnée, aucun moyen de s'en apercevoir.
- **`overpass.openstreetmap.fr`** — de loin la plus rapide (6 communes en 6 s là
  où `overpass-api.de` échouait la moitié du temps), mais elle répond
  « *This service is only available to white-listed usages* ». Elle filtre par
  User-Agent et bloque justement celui de QGIS. Service réservé aux projets
  OSM-FR : nous n'y avons pas droit, et y passer en maquillant l'agent serait
  à la fois abusif et cassable du jour au lendemain.

## 5. Le gel de QGIS, et pourquoi il ne peut plus se reproduire

**Symptôme (2026-09-08).** « Ajout aux communes existantes » sur Le Mans et ses
voisines : QGIS figé, déclaré « ne répond pas » par Windows, aucune progression,
bouton Annuler inopérant — fermeture forcée obligatoire. Alors qu'il ne
s'agissait que de récupérer quelques milliers de points.

**Trois causes cumulées :**

1. **Une requête Overpass par commune.** Mesuré sur Le Mans + 9 voisines :
   **7 requêtes sur 10 refusées** (504), ~25 s chacune. Les instances publiques
   ne tiennent pas ce rythme.
2. **Le coût d'un abandon repayé à chaque commune.** 4 tentatives × 300 s de
   délai de lecture + 31 s de temporisation ≈ 20 min par commune, × 20 communes.
   C'est cela, l'« indéfiniment ».
3. **Le thread graphique bloqué.** `requests.get` bloque son thread, et les
   algorithmes marqués `FlagNoThreading` — ce que sont tous ceux d'ici, pour
   pouvoir afficher des dialogues — tournent dans le thread graphique. Rien ne
   dépilait les événements Qt : fenêtre gelée, et `feedback.isCanceled()` ne
   pouvait jamais devenir vrai puisque le clic sur Annuler n'était pas traité.

**Quatre correctifs :**

- **`QgsBlockingNetworkRequest` à la place de `requests`** (`_overpass_request`)
  — l'attente se fait dans une boucle d'événements Qt imbriquée : la fenêtre
  continue de se redessiner et le `feedback` transmis interrompt le transfert
  en cours. C'est ce qui rend le bouton Annuler à nouveau utile.
- **`_prefetch_osm_trees`** — les deux chemins par lot calculent l'emprise
  englobant toutes les communes et n'émettent **qu'une seule requête** ; chaque
  commune n'a plus qu'à découper. Le Mans + 9 voisines : 1 requête au lieu de
  10, découpage à 0,00–0,02 s par commune. Renonce au regroupement au-delà de
  `_OVERPASS_PREFETCH_MAX_KM2` (2 500 km²), sans quoi la réponse serait énorme
  et refusée. Même esprit que `_prefetch_sirene`.
- **Disjoncteur** — au premier abandon définitif, `_overpass_down` coupe
  Overpass pour **toute l'exécution** : les communes suivantes sont ignorées
  immédiatement au lieu de repayer l'abandon.
- **Bornes courtes** — `setTransferTimeout` à 45 s par requête (fixé sur la
  requête, pas dans le réglage réseau global de QGIS, qu'on ne touche pas),
  3 tentatives, temporisations 2 s puis 5 s. Coût maximal d'un abandon :
  ≈ 2 min 22, une fois par exécution.

`_sleep_cancellable` dépile désormais les événements Qt pendant sa
temporisation — il servait déjà aux reprises WFS, qui en profitent aussi.

**Après correctif**, même scénario : 66 s au total pour les 10 communes, dont
64 s passées dans Overpass (un 504 puis un dépassement de délai avant que la
3ᵉ tentative n'aboutisse), interface vivante et annulable de bout en bout.

## 6. Deux pièges Overpass

**a. Troncature silencieuse par le champ `remark`.** Quand une requête dépasse
son quota de temps ou de mémoire, Overpass renvoie un JSON **valide** contenant
les éléments déjà collectés et signale l'incident dans un champ `remark`. Même
piège que les pages GML corrompues de la Géoplateforme (voir
`WFS ROBUSTESSE.md`) : sans test sur ce champ on charge une couche amputée sans
le moindre signe. `_overpass_fetch` traite `remark` comme un échec et réessaie.

**b. Le dépassement de délai qui se fait passer pour un succès.** Quand
`setTransferTimeout` coupe le transfert, `QgsBlockingNetworkRequest.get()`
renvoie `NoError`, avec un statut HTTP `None` et un corps vide. Se fier au seul
code de retour ferait passer un abandon pour une réponse valide, donc pour une
commune sans arbre. `_overpass_request` teste le statut, pas le code de retour.

*Pour mémoire, un piège désamorcé :* `overpass-api.de` refuse l'agent par défaut
de la bibliothèque `requests` (`python-requests/2.x`) avec un **HTTP 406**,
quelle que soit la requête. Sans objet depuis le passage à
`QgsBlockingNetworkRequest` — `QgsNetworkAccessManager` impose de toute façon
son propre agent (`Mozilla/5.0 QGIS/34015/…`), et `setHeader` sur l'agent est
purement ignoré. À se rappeler si l'on revenait un jour à `requests`.

**Un échec définitif n'interrompt pas l'import** (contrairement au WFS) : il
journalise un avertissement et la couche est simplement absente. Un échec
Overpass ne produit jamais de couche partielle d'apparence normale ; perdre
l'import complet d'une commune pour une couche d'agrément serait disproportionné.

## 7. Attributs conservés

`osm_id`, `leaf_type`, `genus`, `species`, `species_fr`, `name`, `height`,
`circumference`, `denotation` — stockés **en texte, sans conversion**. Les
valeurs OSM sont libres (« 12 », « 12 m », « circa 3 ») : un parsing numérique
strict rejetterait des entités valides pour un gain nul, la couche servant à
dessiner et non à calculer. Les `:` deviennent `_` (`species:fr` →
`species_fr`) pour rester utilisables sans guillemets dans les expressions QGIS.

## 8. Tests

`tests/test_osm_trees.py`, à lancer avec le Python de QGIS :

```
python-qgis-ltr.bat tests/test_osm_trees.py          # les 12 tests
python-qgis-ltr.bat tests/test_osm_trees.py T1 T10   # une sélection
```

Les pannes sont injectées en remplaçant `_overpass_request`, seule méthode qui
touche au réseau — aucun mandataire à lancer, contrairement au test WFS. Sont
couverts : chargement réel, mappage des attributs, `remark`, page HTML en 200,
bascule de serveur, commune sans arbre, symbologie, disjoncteur, emprise
groupée, communes trop dispersées, et la protection des styles règle-par-règle
dans le dialogue.

## 9. Pistes non retenues

- **Alignements** (`natural=tree_row`, des lignes) : hors périmètre ici, mais
  c'est l'extension la plus naturelle — même requête, géométrie linéaire.
- **Arbres remarquables** (`denotation=natural_monument|landmark`) : l'attribut
  est chargé, une règle dédiée serait triviale à ajouter.
- **Inventaires communaux** (Paris ~219 000 arbres avec essence, hauteur,
  circonférence) : bien plus riches, mais un portail et un schéma par ville.
- **LiDAR HD** : la seule voie vers l'exhaustivité (jardins privés, rural), au
  prix d'une segmentation de nuages de points — un chantier hors ligne, pas une
  couche d'import.
