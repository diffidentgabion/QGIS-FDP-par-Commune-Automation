# -*- coding: utf-8 -*-
"""
Axonometric Building Volume — Génération de volumes axonométriques de bâtiments
Script QGIS Processing Toolbox

Installation :
    Traitement > Options > Traitement > Scripts > Dossiers des scripts
    → pointer vers le dossier contenant ce fichier, puis recharger les fournisseurs.
"""

from qgis.core import (
    NULL,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsFillSymbol,
    QgsFeatureRequest,
    QgsGeometry,
    QgsLineString,
    QgsLineSymbol,
    QgsMultiLineString,
    QgsPoint,
    QgsPointXY,
    QgsPolygon,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProperty,
    QgsRenderContext,
    QgsRuleBasedRenderer,
    QgsSimpleLineSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsSymbolLayer,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.PyQt.QtGui import QColor

_GROUP_NAME    = "Axo"
_FALLBACK_COLOR = QColor("#B0AECA")


# ──────────────────────────────────────────────────────────────────────
# Colour helpers
# ──────────────────────────────────────────────────────────────────────

def _start_renderer(layer):
    """
    Clone the layer's renderer and start it with a minimal render context.
    Returns (renderer, render_context), or (None, None) if unavailable.
    The caller must call renderer.stopRender(render_context) when done.
    """
    if layer is None or layer.renderer() is None:
        return None, None
    renderer = layer.renderer().clone()
    render_context = QgsRenderContext()
    renderer.startRender(render_context, layer.fields())
    return renderer, render_context


def _color_for_feature(renderer, render_context, feature):
    """
    Return the QColor the renderer would apply to this feature.
    Handles all renderer types (graduated, categorized, rule-based, single).
    Falls back to _FALLBACK_COLOR if no symbol matches.
    """
    if renderer is None:
        return _FALLBACK_COLOR
    sym = renderer.symbolForFeature(feature, render_context)
    return sym.color() if sym else _FALLBACK_COLOR


def _darken(color, amount):
    """Scale RGB channels down by `amount` (0.0–1.0). amount=0.25 → 25 % darker."""
    f = max(0.0, 1.0 - amount)
    return QColor(
        max(0, int(color.red()   * f)),
        max(0, int(color.green() * f)),
        max(0, int(color.blue()  * f)),
        color.alpha(),
    )


def _color_str(color):
    """'R,G,B,A' string for QGIS data-defined color properties."""
    return f"{color.red()},{color.green()},{color.blue()},{color.alpha()}"


# ──────────────────────────────────────────────────────────────────────
# Styling helpers
# ──────────────────────────────────────────────────────────────────────

def _painter_order_by():
    """ORDER BY sort_y DESC — north buildings underneath, south on top."""
    return QgsFeatureRequest.OrderBy([
        QgsFeatureRequest.OrderByClause("sort_y", ascending=False)
    ])


def _apply_line_style(layer):
    sym_layer = QgsSimpleLineSymbolLayer()
    sym_layer.setColor(QColor(0, 0, 0))
    sym_layer.setWidth(0.15)
    sym_layer.setWidthUnit(QgsUnitTypes.RenderMillimeters)
    symbol = QgsLineSymbol()
    symbol.changeSymbolLayer(0, sym_layer)
    renderer = QgsSingleSymbolRenderer(symbol)
    renderer.setOrderBy(_painter_order_by())
    renderer.setOrderByEnabled(True)
    layer.setRenderer(renderer)


def _apply_fill_style(layer):
    def _make_rule(label, filter_expr):
        sym = QgsFillSymbol.createSimple({"color": "0,0,0,255", "outline_style": "no"})
        sym.symbolLayer(0).setDataDefinedProperty(
            QgsSymbolLayer.PropertyFillColor,
            QgsProperty.fromExpression('"color"'),
        )
        rule = QgsRuleBasedRenderer.Rule(sym)
        rule.setLabel(label)
        rule.setFilterExpression(filter_expr)
        return rule

    root = QgsRuleBasedRenderer.Rule(None)
    root.appendChild(_make_rule("Façades",  "\"face_type\" = 'wall'"))
    root.appendChild(_make_rule("Toitures", "\"face_type\" = 'roof'"))
    renderer = QgsRuleBasedRenderer(root)
    renderer.setOrderBy(_painter_order_by())
    renderer.setOrderByEnabled(True)
    layer.setRenderer(renderer)


def _register(context, layer):
    """Add a memory layer to the processing temp store and schedule it for
    loading into the project under the 'Axo' group."""
    context.temporaryLayerStore().addMapLayer(layer)
    details = QgsProcessingContext.LayerDetails(layer.name(), context.project())
    details.groupName = _GROUP_NAME
    context.addLayerToLoadOnCompletion(layer.id(), details)


# ──────────────────────────────────────────────────────────────────────
# Algorithm
# ──────────────────────────────────────────────────────────────────────

class AxonoBatiments(QgsProcessingAlgorithm):

    INPUT              = "INPUT"
    EXAGGERATION       = "EXAGGERATION"
    FALLBACK_PER_FLOOR = "FALLBACK_PER_FLOOR"
    DEFAULT_HEIGHT     = "DEFAULT_HEIGHT"
    CONTOURS           = "CONTOURS"
    FILL_SURFACES      = "FILL_SURFACES"
    SHADE_AMOUNT       = "SHADE_AMOUNT"

    def tr(self, string):
        return QCoreApplication.translate("AxonoBatiments", string)

    def createInstance(self):
        return AxonoBatiments()

    def name(self):
        return "axonobatiments"

    def displayName(self):
        return self.tr("Volumes axonométriques de bâtiments")

    def group(self):
        return self.tr("FDP par Commune")

    def groupId(self):
        return "fdpparcommune"

    def shortHelpString(self):
        return self.tr(
            "Génère une représentation axonométrique en vue de dessus pour chaque bâtiment "
            "d'une ou plusieurs couches polygonales avec les attributs BD TOPO hauteur/nombre_d_etages.\n\n"
            "Chaque couche source produit ses propres couches contours (+ surfaces si activé), "
            "regroupées dans un groupe « Axo ». "
            "Les bâtiments sont rendus selon un algorithme du peintre (sud devant, nord derrière). "
            "La couleur est lue depuis la symbologie de chaque couche source, "
            "les façades assombries d'un pourcentage réglable."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT,
                self.tr("Couches bâtiments (polygones)"),
                layerType=QgsProcessing.TypeVectorPolygon,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.EXAGGERATION,
                self.tr("Exagération verticale"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.0,
                minValue=0.01,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.FALLBACK_PER_FLOOR,
                self.tr("Hauteur de repli par étage (m)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=3.0,
                minValue=0.01,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.DEFAULT_HEIGHT,
                self.tr("Hauteur par défaut (m)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=3.0,
                minValue=0.01,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.CONTOURS,
                self.tr("Générer les contours (arêtes fil de fer)"),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.FILL_SURFACES,
                self.tr("Remplir les surfaces (toiture + façades visibles)"),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.SHADE_AMOUNT,
                self.tr("Assombrissement des façades (%)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=25.0,
                minValue=0.0,
                maxValue=90.0,
            )
        )

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------

    @staticmethod
    def _line_fields():
        fields = QgsFields()
        fields.append(QgsField("sort_y", QVariant.Double))
        return fields

    @staticmethod
    def _fill_fields():
        fields = QgsFields()
        fields.append(QgsField("face_type", QVariant.String))
        fields.append(QgsField("sort_y",    QVariant.Double))
        fields.append(QgsField("color",     QVariant.String))
        return fields

    # ------------------------------------------------------------------
    # Main algorithm
    # ------------------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT, context)
        if not layers:
            raise QgsProcessingException(self.tr("Aucune couche source fournie."))

        exaggeration       = self.parameterAsDouble(parameters, self.EXAGGERATION,       context)
        fallback_per_floor = self.parameterAsDouble(parameters, self.FALLBACK_PER_FLOOR,  context)
        default_height     = self.parameterAsDouble(parameters, self.DEFAULT_HEIGHT,     context)
        contours           = self.parameterAsBool  (parameters, self.CONTOURS,           context)
        fill_surfaces      = self.parameterAsBool  (parameters, self.FILL_SURFACES,      context)
        shade_pct          = self.parameterAsDouble(parameters, self.SHADE_AMOUNT,       context)

        line_fields = self._line_fields()
        fill_fields = self._fill_fields()

        total = sum(layer.featureCount() for layer in layers)
        count = 0

        for layer in layers:
            crs_id      = layer.crs().authid()
            src_name    = layer.name()
            field_names = layer.fields().names()

            # ── Create output memory layers for this source layer ─────
            line_layer = None
            line_pr    = None
            if contours:
                line_layer = QgsVectorLayer(
                    f"MultiLineString?crs={crs_id}",
                    f"{src_name} — contours",
                    "memory",
                )
                line_pr = line_layer.dataProvider()
                line_pr.addAttributes(line_fields.toList())
                line_layer.updateFields()

            fill_layer = None
            fill_pr    = None
            if fill_surfaces:
                fill_layer = QgsVectorLayer(
                    f"Polygon?crs={crs_id}",
                    f"{src_name} — surfaces",
                    "memory",
                )
                fill_pr = fill_layer.dataProvider()
                fill_pr.addAttributes(fill_fields.toList())
                fill_layer.updateFields()

            # ── Iterate features ──────────────────────────────────────
            renderer, render_ctx = _start_renderer(layer)

            for feature in layer.getFeatures():
                if feedback.isCanceled():
                    break

                roof_color     = _color_for_feature(renderer, render_ctx, feature)
                wall_color     = _darken(roof_color, shade_pct / 100.0)
                roof_color_str = _color_str(roof_color)
                wall_color_str = _color_str(wall_color)

                geom = feature.geometry()
                if geom is None or geom.isEmpty():
                    count += 1
                    continue

                rings = []
                if geom.isMultipart():
                    for part in geom.asMultiPolygon():
                        if part:
                            rings.append(part[0])
                else:
                    poly = geom.asPolygon()
                    if poly:
                        rings.append(poly[0])

                if not rings:
                    count += 1
                    continue

                # ── Effective height ──────────────────────────────────
                hauteur   = feature["hauteur"]         if "hauteur"         in field_names else None
                nb_etages = feature["nombre_d_etages"] if "nombre_d_etages" in field_names else None

                if hauteur is not None and hauteur != NULL and float(hauteur) > 0:
                    effective_height = float(hauteur) * exaggeration
                elif nb_etages is not None and nb_etages != NULL and int(nb_etages) > 0:
                    effective_height = int(nb_etages) * fallback_per_floor * exaggeration
                else:
                    effective_height = default_height * exaggeration

                # ── Painter sort key ──────────────────────────────────
                centroid_y  = geom.centroid().asPoint().y()
                wall_sort_y = centroid_y + 1.0
                roof_sort_y = centroid_y

                # ── Line geometry ─────────────────────────────────────
                multi_ls = QgsMultiLineString()

                for ring in rings:
                    floor_pts = ring[:-1]
                    if len(floor_pts) < 3:
                        continue

                    multi_ls.addGeometry(QgsLineString(
                        [QgsPoint(p.x(), p.y()) for p in floor_pts]
                        + [QgsPoint(floor_pts[0].x(), floor_pts[0].y())]
                    ))

                    if effective_height > 0:
                        roof_pts = [QgsPointXY(p.x(), p.y() + effective_height) for p in floor_pts]

                        multi_ls.addGeometry(QgsLineString(
                            [QgsPoint(p.x(), p.y()) for p in roof_pts]
                            + [QgsPoint(roof_pts[0].x(), roof_pts[0].y())]
                        ))

                        roof_polygon  = QgsGeometry.fromPolygonXY([roof_pts + [roof_pts[0]]])
                        visible_edges = []
                        for fp, rp in zip(floor_pts, roof_pts):
                            visible = not roof_polygon.contains(QgsGeometry.fromPointXY(rp))
                            if visible:
                                multi_ls.addGeometry(
                                    QgsLineString([QgsPoint(fp.x(), fp.y()), QgsPoint(rp.x(), rp.y())])
                                )
                            visible_edges.append(visible)

                        # ── Fill faces ────────────────────────────────
                        if fill_pr:
                            n = len(floor_pts)
                            for i in range(n):
                                j = (i + 1) % n
                                if visible_edges[i] or visible_edges[j]:
                                    fp_i, fp_j = floor_pts[i], floor_pts[j]
                                    rp_i, rp_j = roof_pts[i],  roof_pts[j]
                                    side_ring = QgsLineString([
                                        QgsPoint(fp_i.x(), fp_i.y()),
                                        QgsPoint(fp_j.x(), fp_j.y()),
                                        QgsPoint(rp_j.x(), rp_j.y()),
                                        QgsPoint(rp_i.x(), rp_i.y()),
                                        QgsPoint(fp_i.x(), fp_i.y()),
                                    ])
                                    side_face = QgsPolygon()
                                    side_face.setExteriorRing(side_ring)
                                    wf = QgsFeature(fill_fields)
                                    wf["face_type"] = "wall"
                                    wf["sort_y"]    = wall_sort_y
                                    wf["color"]     = wall_color_str
                                    wf.setGeometry(QgsGeometry(side_face))
                                    fill_pr.addFeature(wf)

                            roof_ring = QgsLineString(
                                [QgsPoint(p.x(), p.y()) for p in roof_pts]
                                + [QgsPoint(roof_pts[0].x(), roof_pts[0].y())]
                            )
                            roof_face = QgsPolygon()
                            roof_face.setExteriorRing(roof_ring)
                            rf = QgsFeature(fill_fields)
                            rf["face_type"] = "roof"
                            rf["sort_y"]    = roof_sort_y
                            rf["color"]     = roof_color_str
                            rf.setGeometry(QgsGeometry(roof_face))
                            fill_pr.addFeature(rf)

                if line_pr:
                    line_feat = QgsFeature(line_fields)
                    line_feat["sort_y"] = centroid_y
                    line_feat.setGeometry(QgsGeometry(multi_ls))
                    line_pr.addFeature(line_feat)

                count += 1
                if count % 100 == 0:
                    feedback.setProgress(int(count / total * 100) if total > 0 else 0)

            if renderer:
                renderer.stopRender(render_ctx)

            # ── Finalise and register output layers ───────────────────
            if line_layer:
                line_layer.updateExtents()
                _apply_line_style(line_layer)
                _register(context, line_layer)

            if fill_layer:
                fill_layer.updateExtents()
                _apply_fill_style(fill_layer)
                _register(context, fill_layer)

            if feedback.isCanceled():
                break

        feedback.setProgress(100)
        return {}
