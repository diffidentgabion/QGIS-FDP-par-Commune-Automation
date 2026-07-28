"""
QGIS Layer-by-Layer PDF Exporter  v6
Compatible with QGIS 3.4 LTS+

New in v6:
  - Commune groups (custom property "fdp_insee") are tagged with an "insee"
    field in manifest.json, so the Illustrator importer knows which top-level
    groups are communes and can regroup them theme-first safely — and leaves
    everything else (e.g. Axo stacks) untouched.

Lives in QGIS-FDP-par-Commune-Automation/export/ (single home).

Run this in the QGIS Python Console:
  Plugins > Python Console > Show Editor > paste > Run

New in v5:
  - Rule-based renderers (QgsRuleBasedRenderer) are detected on vector layers.
    Each named, active rule is exported as its own PDF and appears as a named
    sublayer inside the parent layer group in Illustrator.
  - Layers with other renderer types (single symbol, categorized, graduated)
    continue to export as a single PDF as before.
  - Rules are isolated by cloning the renderer, disabling all rules except the
    target, applying it, exporting, then restoring the original renderer.
    This is non-destructive — the project renderer is always restored even if
    export fails.
"""

import os
import json
import copy
from qgis.core import (
    QgsProject,
    QgsLayerTreeLayer,
    QgsLayerTreeGroup,
    QgsPrintLayout,
    QgsLayoutItemMap,
    QgsLayoutExporter,
    QgsLayoutSize,
    QgsUnitTypes,
    QgsFillSymbol,
    QgsSimpleFillSymbolLayer,
    QgsRuleBasedRenderer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QRectF, Qt
from qgis.PyQt.QtGui import QColor
from qgis.utils import iface


# ── Config ────────────────────────────────────────────────────────────────────

DPI            = 300
PADDING        = 0
OUTPUT_SUBDIR  = "layer_pdfs"

# ── State ─────────────────────────────────────────────────────────────────────

_counter       = [0]
_group_counter = [0]


# ── Helpers ───────────────────────────────────────────────────────────────────

def project_dir():
    path = QgsProject.instance().absoluteFilePath()
    if not path:
        raise RuntimeError("Project is not saved. Save the .qgz file first.")
    return os.path.dirname(path)


def safe_name(name):
    keep = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- ")
    return "".join(c if c in keep else "_" for c in name).strip()


def canvas_extent():
    # When the canvas is rotated, canvas.extent() returns the axis-aligned
    # bounding box of the rotated view -- larger than the actual visible frame.
    # Instead derive the true visible extent from center + mapUnitsPerPixel,
    # which combined with setMapRotation() gives the correct rotated framing.
    canvas   = iface.mapCanvas()
    center   = canvas.center()
    dpm      = canvas.mapUnitsPerPixel()
    canvas_w = canvas.width()  or 1
    canvas_h = canvas.height() or 1
    ext_w    = dpm * canvas_w
    ext_h    = dpm * canvas_h
    from qgis.core import QgsRectangle
    ext = QgsRectangle(
        center.x() - ext_w / 2,
        center.y() - ext_h / 2,
        center.x() + ext_w / 2,
        center.y() + ext_h / 2,
    )
    if PADDING:
        ext.grow(PADDING)
    return ext



def make_transparent_fill_symbol():
    sym_layer = QgsSimpleFillSymbolLayer()
    sym_layer.setFillColor(QColor(0, 0, 0, 0))
    sym_layer.setStrokeStyle(Qt.NoPen)
    sym_layer.setStrokeColor(QColor(0, 0, 0, 0))
    symbol = QgsFillSymbol()
    symbol.changeSymbolLayer(0, sym_layer)
    return symbol


def group_display_name(group_node):
    name = group_node.name().strip()
    if name and name not in ("<calque>", "<layer>"):
        return name
    _group_counter[0] += 1
    for child in group_node.children():
        child_name = getattr(child, "name", lambda: "")().strip()
        if child_name and child_name not in ("<calque>", "<layer>"):
            return f"Group_{_group_counter[0]} ({child_name}...)"
    return f"Group_{_group_counter[0]}"


def make_layout():
    """Create a fresh transparent layout for one export.
    Page aspect ratio comes from the canvas pixel dimensions — these match
    the actual visible frame including rotation. The 50% offset bug was caused
    by the JSX using these values for document sizing; fixed there instead.
    """
    project   = QgsProject.instance()
    layout    = QgsPrintLayout(project)
    layout.initializeDefaults()

    canvas    = iface.mapCanvas()
    canvas_w  = canvas.width()  or 1
    canvas_h  = canvas.height() or 1
    page_w_mm = 400.0
    page_h_mm = page_w_mm * (canvas_h / canvas_w)

    page = layout.pageCollection().pages()[0]
    page.setPageSize(QgsLayoutSize(page_w_mm, page_h_mm, QgsUnitTypes.LayoutMillimeters))
    layout.pageCollection().setPageStyleSymbol(make_transparent_fill_symbol())

    return layout, page_w_mm, page_h_mm


def export_layer_to_pdf(output_path, layer, extent):
    """Export a layer (with whatever renderer is currently set) to PDF."""
    layout, page_w_mm, page_h_mm = make_layout()

    map_item = QgsLayoutItemMap(layout)
    map_item.attemptSetSceneRect(QRectF(0, 0, page_w_mm, page_h_mm))
    map_item.setFrameEnabled(False)
    map_item.setBackgroundEnabled(False)
    map_item.setExtent(extent)
    map_item.setMapRotation(iface.mapCanvas().rotation())
    map_item.setLayers([layer])
    layout.addLayoutItem(map_item)
    map_item.refresh()

    exporter     = QgsLayoutExporter(layout)
    pdf_settings = QgsLayoutExporter.PdfExportSettings()
    pdf_settings.dpi               = DPI
    pdf_settings.forceVectorOutput = True
    pdf_settings.exportMetadata    = True

    result = exporter.exportToPdf(output_path, pdf_settings)
    if result != QgsLayoutExporter.Success:
        raise RuntimeError(f"PDF export failed (code {result})")


# ── Rule-based renderer handling ──────────────────────────────────────────────

def collect_rule_data(rule):
    """Collect leaf rules as plain dicts {label, key} — never store
    Rule object references which Qt can invalidate between calls."""
    result = []
    children = rule.children()
    if not children:
        if rule.active():
            label = rule.label().strip()
            key   = rule.ruleKey()
            if label:
                result.append({"label": label, "key": key})
    else:
        for child in children:
            result.extend(collect_rule_data(child))
    return result


def export_rule_based_layer(layer, out_dir, extent):
    """Export each named rule in a rule-based layer as a separate PDF."""
    renderer = layer.renderer()
    if not isinstance(renderer, QgsRuleBasedRenderer):
        return None

    # Extract label+key as plain Python strings immediately —
    # do not hold Rule object references across the export loop.
    rule_data = collect_rule_data(renderer.rootRule())
    if not rule_data:
        return None

    original_renderer = renderer.clone()
    rule_entries = []

    for rd in rule_data:
        rule_label = rd["label"]
        target_key = rd["key"]

        _counter[0] += 1
        fname = f"{_counter[0]:04d}_{safe_name(layer.name())}_{safe_name(rule_label)}.pdf"
        fpath = os.path.join(out_dir, fname)

        try:
            isolated = original_renderer.clone()
            _set_only_rule_active(isolated.rootRule(), target_key)
            layer.setRenderer(isolated)
            layer.triggerRepaint()

            export_layer_to_pdf(fpath, layer, extent)
            print(f"    ✓ [{_counter[0]:04d}] {layer.name()} / {rule_label}")

            rule_entries.append({
                "type":          "layer",
                "name":          rule_label,
                "file":          fname,
                "geometry_type": int(layer.type()),
            })

        except Exception as e:
            print(f"    ✗ [{_counter[0]:04d}] {layer.name()} / {rule_label} — {e}")
            rule_entries.append({
                "type":  "layer",
                "name":  rule_label + " ⚠ export failed",
                "file":  None,
                "error": str(e),
            })
        finally:
            layer.setRenderer(original_renderer.clone())
            layer.triggerRepaint()

    return rule_entries




def _set_only_rule_active(rule, target_key):
    """
    Recursively walk a cloned rule tree.
    Activate only the rule whose ruleKey matches target_key.
    Deactivate all others. Groups (non-leaf rules) stay active so
    the target can be reached.
    """
    children = rule.children()
    if not children:
        # Leaf — activate only if it's the target
        rule.setActive(rule.ruleKey() == target_key)
    else:
        # Non-leaf — keep active, recurse
        rule.setActive(True)
        for child in children:
            _set_only_rule_active(child, target_key)


# ── Tree walker ───────────────────────────────────────────────────────────────

def walk_tree(node, out_dir, extent, manifest_node):
    children_manifest = []
    child_nodes = node.children() if hasattr(node, "children") else []

    for child in child_nodes:
        if not child.itemVisibilityChecked():
            continue

        if isinstance(child, QgsLayerTreeGroup):
            name = group_display_name(child)
            group_entry = {"type": "group", "name": name, "children": []}
            # Commune groups created by fdp_par_commune.py carry this custom
            # property. The Illustrator importer uses it to know which
            # top-level groups are safe to regroup theme-first.
            insee = child.customProperty("fdp_insee")
            if insee:
                group_entry["insee"] = str(insee)
            walk_tree(child, out_dir, extent, group_entry)
            if group_entry["children"]:
                children_manifest.append(group_entry)

        elif isinstance(child, QgsLayerTreeLayer):
            layer = child.layer()
            if layer is None:
                continue

            # ── Rule-based vector layer ───────────────────────────────────────
            if isinstance(layer, QgsVectorLayer):
                rule_entries = export_rule_based_layer(layer, out_dir, extent)
                if rule_entries is not None:
                    # Wrap rule PDFs in a group named after the layer
                    children_manifest.append({
                        "type":     "group",
                        "name":     layer.name(),
                        "children": rule_entries,
                    })
                    continue  # skip the plain single-PDF export below

            # ── All other layers (or rule-based with no named rules) ──────────
            _counter[0] += 1
            fname = f"{_counter[0]:04d}_{safe_name(layer.name())}.pdf"
            fpath = os.path.join(out_dir, fname)

            try:
                export_layer_to_pdf(fpath, layer, extent)
                print(f"  ✓ [{_counter[0]:04d}] {layer.name()}")
                children_manifest.append({
                    "type":          "layer",
                    "name":          layer.name(),
                    "file":          fname,
                    "geometry_type": int(layer.type()),
                })
            except Exception as e:
                print(f"  ✗ [{_counter[0]:04d}] {layer.name()} — {e}")
                children_manifest.append({
                    "type":  "layer",
                    "name":  layer.name() + " ⚠ export failed",
                    "file":  None,
                    "error": str(e),
                })

    manifest_node["children"] = children_manifest


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    project = QgsProject.instance()
    root    = project.layerTreeRoot()

    out_dir = os.path.join(project_dir(), OUTPUT_SUBDIR)
    os.makedirs(out_dir, exist_ok=True)

    extent = canvas_extent()
    print(f"\n▶ Exporting to: {out_dir}")
    print(f"  Extent : {extent.xMinimum():.2f}, {extent.yMinimum():.2f} → "
          f"{extent.xMaximum():.2f}, {extent.yMaximum():.2f}")
    print(f"  DPI    : {DPI}\n")

    _counter[0]       = 0
    _group_counter[0] = 0

    canvas    = iface.mapCanvas()
    canvas_w  = canvas.width()  or 1
    canvas_h  = canvas.height() or 1
    page_w_mm = 400.0
    page_h_mm = page_w_mm * (canvas_h / canvas_w)

    manifest = {
        "type":             "root",
        "name":             project.title() or os.path.basename(
                                project.absoluteFilePath()),
        "canvas_width_px":  canvas_w,
        "canvas_height_px": canvas_h,
        "page_width_mm":    page_w_mm,
        "page_height_mm":   page_h_mm,
        "canvas_rotation":  canvas.rotation(),
        "dpi":              DPI,
        "children":         [],
    }

    walk_tree(root, out_dir, extent, manifest)

    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Done — {_counter[0]} PDFs exported.")
    print(f"   Manifest : {manifest_path}")
    print(f"\nNow run qgis_to_illustrator.jsx in Illustrator.")


run()
