# -*- coding: utf-8 -*-
"""
zone_buildings.py — Appariement bâtiments × zones d'activité et d'intérêt (ZAI)

Expose :
    ZAI_CATEGORIES              list[dict]  — 8 catégories ZAI BDTOPO + couleurs
    generate_gradient(base_color_hex, n_steps) -> list[QColor]
    build_zone_activity_layers(buildings_layer, zai_layer, feedback)
        -> list[tuple[str, QgsVectorLayer]]
           Chaque tuple : (category_label, layer). Ordonné par ZAI_CATEGORIES,
           puis par intensité au sein de chaque catégorie (lightest first).

Dépendance QGIS uniquement — pas d'import relatif, compatible scripts Processing.

Notes sur les attributs BDTOPO_V3:zone_d_activite_ou_d_interet :
  - categorie   : 8 valeurs larges (Science et enseignement, Santé, …)
  - nature      : type au sein de la catégorie (Collège, Hôpital, …)
  - nature_detaillee : précision supplémentaire (Ecole maternelle, Eglise, …),
                      souvent vide — on utilise alors nature comme repli.
  - fictif      : "Vrai" pour les zones fictives à exclure

Discriminateur retenu : `nature_detaillee` si non vide, sinon `nature`.
Cette valeur composite est appelée «label» dans le code.
natures_ordered liste les labels attendus par ordre d'intensité visuelle
(lightest → darkest). Tout label non listé va dans la couche catch-all.
"""

from qgis.core import (
    QgsFeatureRequest,
    QgsFillSymbol,
    QgsSingleSymbolRenderer,
    QgsSpatialIndex,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor


def _field_str(v) -> str:
    """Convertit une valeur de champ QGIS en str, '' pour None ou PyQGIS NULL."""
    if v is None:
        return ""
    s = str(v).strip()
    # str(PyQGIS NULL) == "NULL" dans toutes les versions QGIS ;
    # on utilise cette propriété plutôt qu'une comparaison d'identité
    # qui échoue si le provider renvoie un nouveau variant NULL à chaque appel.
    return "" if s == "NULL" else s

# =============================================================================
# Labels nature_detaillee à ignorer — on préfère nature dans ces cas
# =============================================================================
# Pour la plupart des entrées, nature_detaillee est plus précis que nature et
# sert de discriminateur principal. Dans quelques cas il est plus générique ou
# ambigu : on revient alors à nature.
_PREFER_NAT = {
    "Complexe sportif",  # terme générique — moins précis que "Stade"
    "Centre de secours", # synonyme de "Caserne de pompiers" ; on garde le nat plus précis
}

# =============================================================================
# Espaces publics extérieurs — ZAI à afficher comme zones, pas comme bâtiments
# =============================================================================
# Ces natures désignent des espaces ouverts (parcs, places, promenades…) qui ne
# contiennent pas de bâtiments au sens propre. Ils sont exclus de l'attribution
# de bâtiments dans build_zone_activity_layers et rendus directement comme
# polygones ZAI dans build_outdoor_space_layers.
# Ordre : végétal d'abord (vert), puis minéral/civique (beige/gris), puis générique.

_OUTDOOR_PUBLIC_COLORS = {
    # ── Espaces végétalisés ───────────────────────────────────────────────────
    "Parc":             "#52B788",   # vert forêt — parc urbain ou naturel
    "Jardin public":    "#74C69D",   # vert moyen — jardin aménagé
    "Jardins familiaux":"#95D5B2",   # vert clair — jardins partagés/ouvriers
    "Square":           "#B7E4C7",   # vert très clair — petit square planté
    "Promenade":        "#D8F3DC",   # vert pâle — allée arborée
    # ── Espaces minéraux / civiques ───────────────────────────────────────────
    "Place":            "#C9B99A",   # beige pierre — place dallée
    "Parvis":           "#D4C9B0",   # beige clair — parvis d'édifice
    "Esplanade":        "#BFB8A4",   # gris pierre — esplanade dégagée
    "Terrasse":         "#D4B896",   # ocre chaud — terrasse minérale
    # ── Générique ─────────────────────────────────────────────────────────────
    "Espace public":    "#ADADAD",   # gris neutre — espace non qualifié
}

_OUTDOOR_PUBLIC = set(_OUTDOOR_PUBLIC_COLORS)

# =============================================================================
# Constantes publiques
# =============================================================================
# Valeurs validées sur un échantillon de 3 000 entités WFS Géoplateforme.
# natures_ordered : labels (natd || nat) du moins intense au plus intense.
# La couleur du gradient va de v=1.0 (lightest, index 0) à v≈0.45 (darkest).

ZAI_CATEGORIES = [
    {
        "categorie": "Science et enseignement",
        "label": "Éducation",
        "base_color": "#FFD166",
        "zone_color": "#FFF0B3",
        # Ordre : maternelle → primaire → secondaire → lycée → supérieur → recherche
        "natures_ordered": [
            "Ecole maternelle",
            "Ecole élémentaire",
            "Ecole primaire",
            "Groupe scolaire",
            "Enseignement primaire",
            "Collège annexe",
            "Collège",
            "Cité scolaire",
            "Lycée professionnel",
            "Lycée",
            "Enseignement supérieur",
            "Ecole d'ingénieurs",
            "Conservatoire national",
            "Université",
            "Institut universitaire de technologie",
            "Unité de formation et de recherche",
            "Centre de recherche",
            "Observatoire",
            "Science",
        ],
        "catch_all_label": "Autre enseignement ou recherche",
    },
    {
        "categorie": "Santé",
        "label": "Santé",
        "base_color": "#06D6A0",
        "zone_color": "#B3F5E6",
        # Ordre : soins légers/long séjour → hospitalisation aiguë
        "natures_ordered": [
            "Maison de retraite",
            "Etablissement de rééducation fonctionnelle",
            "Etablissement de convalescence",
            "Foyer d'accueil médicalisé",
            "Maison de santé pour maladies mentales",
            "Etablissement hospitalier",
            "Hôpital spécialisé",
            "Hôpital militaire",
            "Hôpital",
            "Centre hospitalier régional",
        ],
        "catch_all_label": "Autre santé",
    },
    {
        "categorie": "Administratif ou militaire",
        "label": "Administratif & militaire",
        "base_color": "#C1121F",
        "zone_color": "#F5B3B6",
        # Ordre : services de proximité → institutions nationales/militaires
        "natures_ordered": [
            "Bureau de poste",
            "Centre de secours",
            "Commissariat central",
            "CPAM",
            "Centre des finances publiques",
            "Chambre de commerce et d'industrie",
            "Mairie annexe",
            "Mairie d'arrondissement",
            "Mairie",
            "Divers public ou administratif",
            "Cité administrative",
            "Gendarmerie",
            "Conseil de prud'hommes",
            "Tribunal de commerce",
            "Palais de justice",
            "Caserne de pompiers",
            "Caserne",
            "Préfecture",
            "Préfecture de police",
            "Administration centrale de l'Etat",
            "Préfecture de région",
            "Enceinte militaire",
            "Fort",
            "Maison d'arrêt",
            "Institution européenne",
        ],
        "catch_all_label": "Autre administratif",
    },
    {
        "categorie": "Industriel et commercial",
        "label": "Industriel & commercial",
        "base_color": "#8B5E3C",
        "zone_color": "#E8D5C4",
        # Ordre : commerce de détail → industrie → zones
        "natures_ordered": [
            "Centre commercial",
            "Halle",
            "Divers commercial",
            "Marché",
            "Déchèterie",
            "Dépôt d'hydrocarbures",
            "Divers industriel",
            "Fabrique",
            "Usine",
            "Centrale thermique",
            "Zone d'activités",
            "Parc d'activités tertiaires",
            "Zone industrielle",
        ],
        "catch_all_label": "Autre industriel",
    },
    {
        "categorie": "Culture et loisirs",
        "label": "Culture & loisirs",
        "base_color": "#118AB2",
        "zone_color": "#B3DFF0",
        # Ordre : loisirs simples → patrimoine/culture → grandes salles/parcs
        "natures_ordered": [
            "Moulin à vent",
            "Statue",
            "Monument aux morts",
            "Monument",
            "Point de vue",
            "Table d'orientation",
            "Bibliothèque",
            "Archives départementales",
            "Archives nationales",
            "Bibliothèque nationale",
            "Centre de documentation",
            "Habitation troglodytique",
            "Théâtre romain",
            "Vestige archéologique",
            "Parc d'attractions",
            "Parc de loisirs",
            "Parc zoologique",
            "Centre culturel",
            "Salle de spectacle",            # natd (preferred over long nat)
            "Salle de spectacle ou conférence",  # nat fallback when natd empty
            "Musée",
            "Parc des expositions",
        ],
        "catch_all_label": "Autre culture et loisirs",
    },
    {
        "categorie": "Sport",
        "label": "Sport",
        "base_color": "#F4A261",
        "zone_color": "#FCDEC4",
        # Ordre : équipements simples → grands complexes
        "natures_ordered": [
            "Terrain de sport",
            "Pelote basque",
            "Gymnase",
            "Complexe sportif couvert",
            "Centre aquatique",
            "Piscine",
            "Golf",
            "Patinoire",
            "Centre équestre",
            "Hippodrome",
            "Complexe sportif",
            "Stade",
            "Autre équipement sportif",
        ],
        "catch_all_label": "Équipement sportif",
    },
    {
        "categorie": "Religieux",
        "label": "Religieux",
        "base_color": "#9B72AA",
        "zone_color": "#E0D0E8",
        # Ordre : funéraire/petit → édifices cultuels → grandes cathédrales
        "natures_ordered": [
            "Tombe",
            "Chapelle funéraire",
            "Crématorium",
            "Cimetière animalier",
            "Tombeau",
            "Oratoire",
            "Temple bouddhiste",
            "Mosquée",
            "Synagogue",
            "Temple protestant",
            "Eglise orthodoxe",
            "Chapelle",
            "Prieuré",
            "Couvent",
            "Monastère",
            "Eglise",
            "Basilique",
            "Abbaye",
            "Cathédrale",
            "Culte chrétien",
            "Culte divers",
            "Culte israélite",
            "Culte musulman",
        ],
        "catch_all_label": "Lieu de culte",
    },
    {
        "categorie": "Gestion des eaux",
        "label": "Gestion des eaux",
        "base_color": "#6baed6",
        "zone_color": "#C4E3F5",
        "natures_ordered": [
            "Station de pompage",
            "Station d'épuration",
            "Usine de production d'eau potable",
        ],
        "catch_all_label": "Autre gestion des eaux",
    },
]

# =============================================================================
# Utilitaire partagé — importé aussi par sirene_buildings.py
# =============================================================================


def generate_gradient(base_color_hex: str, n_steps: int) -> list:
    """
    Retourne n_steps QColor assombrissant progressivement base_color_hex.

    La valeur HSV (v) descend de 1.0 (le plus clair, index 0) à 0.45
    (le plus sombre, index -1) en n_steps incréments égaux.
    Teinte et saturation sont préservées.

    Si n_steps <= 1, retourne une liste d'un seul QColor à base_color_hex.
    """
    base = QColor(base_color_hex)
    h = base.hsvHueF()
    s = base.hsvSaturationF()

    if n_steps <= 1:
        return [QColor(base_color_hex)]

    result = []
    for i in range(n_steps):
        v = 1.0 - (i / (n_steps - 1)) * 0.55
        result.append(QColor.fromHsvF(max(h, 0.0), s, v))
    return result


# =============================================================================
# Fonction publique
# =============================================================================


def build_zone_activity_layers(
    buildings_layer: QgsVectorLayer,
    zai_layer: QgsVectorLayer,
    feedback,
) -> list:
    """
    Pour chaque ZAI, identifie les bâtiments qui l'intersectent.
    Groupe les bâtiments par catégorie + label (nature_detaillee || nature).
    Génère une couche mémoire colorée par label, avec dégradé par intensité.

    Discriminateur : nature_detaillee si non vide, sinon nature.
    Un bâtiment intersectant plusieurs ZAI de catégories différentes est inclus
    dans toutes les catégories correspondantes (exhaustivité par catégorie).

    Retourne list[tuple[str, QgsVectorLayer]] :
      - str = category label (cat["label"]), pour construire les sous-groupes QGIS
      - QgsVectorLayer = couche mémoire prête à ajouter au projet
    Ordre : ZAI_CATEGORIES index croissant, puis lightest-first au sein de chaque
    catégorie (le plus sombre sera donc en haut de la légende QGIS après ajout).
    """
    crs_id = buildings_layer.crs().authid()

    # ── Étape 1 : index spatial + cache géométrie des bâtiments ──────────────
    bld_index = QgsSpatialIndex(buildings_layer.getFeatures())
    bld_geom = {}
    for feat in buildings_layer.getFeatures():
        bld_geom[feat.id()] = feat.geometry()

    if not bld_geom:
        return []

    # ── Étape 2 : itérer les ZAI et apparier les bâtiments ───────────────────
    # building_matches : bld_fid → list[(categorie_str, label_str)]
    # label = nature_detaillee si non vide, sinon nature (discriminateur principal)
    building_matches = {}

    for processed, zai_feat in enumerate(zai_layer.getFeatures()):
        if processed % 200 == 0 and feedback.isCanceled():
            return []

        # Belt-and-suspenders : exclure les fictifs résiduels
        if _field_str(zai_feat["fictif"]) == "Vrai":
            continue

        categorie = _field_str(zai_feat["categorie"])
        nat_str   = _field_str(zai_feat["nature"])
        natd_str  = _field_str(zai_feat["nature_detaillee"])
        # Discriminateur : nature_detaillee sauf si elle est dans _PREFER_NAT
        # (cas où natd est moins précis ou ambigu par rapport à nat).
        label = natd_str if (natd_str and natd_str not in _PREFER_NAT) else nat_str

        if not categorie or not label:
            continue

        # Espaces publics extérieurs → rendus directement comme zones, pas attribués aux bâtiments
        if label in _OUTDOOR_PUBLIC:
            continue

        zai_geom = zai_feat.geometry()
        if not zai_geom or zai_geom.isEmpty():
            continue

        candidates = bld_index.intersects(zai_geom.boundingBox())
        for bld_fid in candidates:
            if zai_geom.intersects(bld_geom[bld_fid]):
                building_matches.setdefault(bld_fid, []).append((categorie, label))

    if not building_matches:
        return []

    # ── Étape 3 : regrouper par catégorie ZAI ────────────────────────────────
    # cat_data : cat_idx → { label_str → set(bld_fid) }
    cat_data   = {i: {} for i in range(len(ZAI_CATEGORIES))}
    cat_lookup = {c["categorie"]: i for i, c in enumerate(ZAI_CATEGORIES)}

    for bld_fid, matches in building_matches.items():
        seen = set()   # évite les doublons (cat_idx, label) pour ce bâtiment
        for categorie, label in matches:
            cat_idx = cat_lookup.get(categorie)
            if cat_idx is None:
                continue
            key = (cat_idx, label)
            if key in seen:
                continue
            seen.add(key)
            cat_data[cat_idx].setdefault(label, set()).add(bld_fid)

    # ── Étapes 4 & 5 : gradient, construction des couches mémoire ────────────
    fields  = buildings_layer.fields()
    results = []   # list[tuple[str, QgsVectorLayer]]

    for cat_idx, cat in enumerate(ZAI_CATEGORIES):
        label_fids = cat_data[cat_idx]
        if not label_fids:
            continue

        natures_ordered = cat["natures_ordered"]
        base_color      = cat["base_color"]
        n_steps         = len(natures_ordered)
        gradient        = generate_gradient(base_color, n_steps) if n_steps > 0 else []

        # Labels connus, dans l'ordre défini (lightest → darkest)
        sublayers = []
        for step_idx, label_name in enumerate(natures_ordered):
            fids = label_fids.get(label_name)
            if not fids:
                continue
            color = gradient[step_idx] if gradient else QColor(base_color)
            sublayers.append((label_name, color, fids))

        # Labels inconnus → catch-all avec base_color
        natures_set    = set(natures_ordered)
        catch_all_fids = set()
        for label_name, fids in label_fids.items():
            if label_name not in natures_set:
                catch_all_fids |= fids
        if catch_all_fids:
            sublayers.append((cat["catch_all_label"], QColor(base_color), catch_all_fids))

        # Créer une couche mémoire par sublayer
        for nature_label, color, fids in sublayers:
            mem_layer = QgsVectorLayer(
                f"Polygon?crs={crs_id}",
                nature_label,   # nom court : le nom de la catégorie est dans le sous-groupe parent
                "memory",
            )
            pr = mem_layer.dataProvider()
            pr.addAttributes(fields.toList())
            mem_layer.updateFields()

            feats = list(buildings_layer.getFeatures(
                QgsFeatureRequest().setFilterFids(list(fids))
            ))
            pr.addFeatures(feats)
            mem_layer.updateExtents()

            c         = color
            color_str = f"{c.red()},{c.green()},{c.blue()},{c.alpha()}"
            sym = QgsFillSymbol.createSimple({
                "color":         color_str,
                "outline_style": "no",
            })
            mem_layer.setRenderer(QgsSingleSymbolRenderer(sym))

            # Tuple (category_label, layer) pour que l'appelant crée les sous-groupes
            results.append((cat["label"], mem_layer))

    return results


# =============================================================================
# Espaces publics extérieurs
# =============================================================================


def build_outdoor_space_layers(zai_layer: QgsVectorLayer, feedback) -> list:
    """
    Retourne une couche mémoire colorée par label pour chaque espace public
    extérieur trouvé dans zai_layer (Parc, Place, Square…).

    Chaque ZAI dont le label (natd || nat) est dans _OUTDOOR_PUBLIC est rendu
    directement comme polygone de zone — aucun appariement bâtiment n'est
    effectué ici.

    Retourne list[QgsVectorLayer] ordonné par l'ordre de _OUTDOOR_PUBLIC_COLORS
    (végétal d'abord, puis minéral/civique, puis générique).
    Les labels sans entité sont omis.
    """
    crs_id = zai_layer.crs().authid()
    fields = zai_layer.fields()

    # Grouper les features par label outdoor
    label_feats: dict = {label: [] for label in _OUTDOOR_PUBLIC_COLORS}

    for processed, feat in enumerate(zai_layer.getFeatures()):
        if processed % 200 == 0 and feedback.isCanceled():
            return []

        if _field_str(feat["fictif"]) == "Vrai":
            continue

        nat_str  = _field_str(feat["nature"])
        natd_str = _field_str(feat["nature_detaillee"])
        label = natd_str if (natd_str and natd_str not in _PREFER_NAT) else nat_str

        if label in label_feats:
            label_feats[label].append(feat)

    results = []
    for label, color_hex in _OUTDOOR_PUBLIC_COLORS.items():
        feats = label_feats[label]
        if not feats:
            continue

        mem_layer = QgsVectorLayer(
            f"Polygon?crs={crs_id}",
            label,
            "memory",
        )
        pr = mem_layer.dataProvider()
        pr.addAttributes(fields.toList())
        mem_layer.updateFields()
        pr.addFeatures(feats)
        mem_layer.updateExtents()

        c   = QColor(color_hex)
        sym = QgsFillSymbol.createSimple({
            "color":         f"{c.red()},{c.green()},{c.blue()},200",
            "outline_style": "no",
        })
        mem_layer.setRenderer(QgsSingleSymbolRenderer(sym))
        results.append(mem_layer)

    return results
