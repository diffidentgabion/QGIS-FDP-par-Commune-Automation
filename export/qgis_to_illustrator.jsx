/**
 * QGIS → Illustrator Layer Importer  v7.1
 * qgis_to_illustrator.jsx
 *
 * New in v7.1:
 *   - Global swatches built from the imported art: one global process swatch
 *     per distinct colour per layer, in a swatch group per theme, with every
 *     path linked to it — editing a swatch recolours that layer across all
 *     communes at once. Layers with > MAX_SWATCHES_PER_LAYER colours
 *     (graduated ramps) are left as is and listed in the final alert.
 *     Needs embedded placement (PLACE_AS_LINKED = false). BUILD_SWATCHES
 *     turns it off.
 *
 * New in v7:
 *   - Purely thematic output: commune groups (and the organisational groups
 *     holding them) no longer appear at ANY level. Every commune's subtree is
 *     merged into one shared theme tree by name, all the way down, so e.g.
 *     "Voirie → Autoroute" is a single layer holding one placed PDF per
 *     commune. Per-commune visibility is left to the commune limits layer.
 *
 * New in v6.4:
 *   - Commune groups nested inside organisational groups (e.g. CTMs in Lyon:
 *     root → CTM → communes) are now found and regrouped theme-first too.
 *     Before, only top-level groups were inspected, so a CTM layout either
 *     imported as-is or had its communes' names mistaken for themes. The
 *     organisational level is kept inside each theme master
 *     (Voirie → CTM → commune).
 *
 * New in v6.3:
 *   - Theme-first regrouping is now applied ONLY to commune groups. It
 *     auto-detects them via the "insee" tag written by exporter v6; on
 *     older manifests it asks first, and "No" imports the tree exactly as
 *     exported. Non-commune root nodes (e.g. Axo building stacks, whose
 *     same-named sibling groups must never be merged) keep their exact
 *     position and internal order.
 *   - Page-size guard: if a placed PDF's native width doesn't match the
 *     manifest page width (wrong sticky "Crop to" Place option), the import
 *     aborts immediately with instructions instead of producing an
 *     out-of-register document. The script never writes Place preferences
 *     itself (v6.1 tried with a guessed undocumented value — never again).
 *   - Final alert lists missing PDFs and placement failures.
 *
 * Lives in QGIS-FDP-par-Commune-Automation/export/ (single home).
 *
 * Run via: File > Scripts > Other Script… > select this file
 *
 * Requires: Adobe Illustrator CC 2018+ (ExtendScript)
 */

// ── Config ────────────────────────────────────────────────────────────────────

var DOC_WIDTH_PT = 841.89; // A3 width in points (297mm)
var PLACE_AS_LINKED = false; // false = embedded, true = linked
var THEME_FIRST = true; // true  = purely thematic tree, communes merged into each layer
//                         false = original commune-first mirror of the QGIS tree
var BUILD_SWATCHES = true; // one global swatch per colour per layer (embedded only)
var MAX_SWATCHES_PER_LAYER = 12; // more colours than this (graduated ramp…) → layer left as is

// ── Globals ───────────────────────────────────────────────────────────────────

var gPlaced = 0;
var gMissing = []; // layer names whose PDF file was absent
var gFailed = []; // layer names where placement threw
var gExpectedWpt = 0; // native PDF page width in pt; 0 disables the size check
var gAbort = false; // set when the user aborts on a size-check failure
var gSwatches = 0; // global swatches created
var gSwatchSkipped = []; // layers left without swatches, with the reason
var gSwatchNames = {}; // "k" + name → true, every swatch name in the document
var gSwatchGroups = {}; // "k" + name → SwatchGroup

// ── Iterative JSON parser ─────────────────────────────────────────────────────
// ExtendScript's eval() uses recursive descent and blows its ~256-frame
// call stack on deeply nested JSON. This parser is fully iterative.

function parseJSON(str) {
  var i = 0;

  function skipWhitespace() {
    while (i < str.length && /\s/.test(str[i])) i++;
  }

  function parseValue() {
    skipWhitespace();
    var c = str[i];
    if (c === "{") return parseObject();
    if (c === "[") return parseArray();
    if (c === '"') return parseString();
    if (c === "t") {
      i += 4;
      return true;
    }
    if (c === "f") {
      i += 5;
      return false;
    }
    if (c === "n") {
      i += 4;
      return null;
    }
    return parseNumber();
  }

  function parseObject() {
    var obj = {};
    i++;
    skipWhitespace();
    if (str[i] === "}") {
      i++;
      return obj;
    }
    while (i < str.length) {
      skipWhitespace();
      var key = parseString();
      skipWhitespace();
      i++; // skip :
      obj[key] = parseValue();
      skipWhitespace();
      if (str[i] === "}") {
        i++;
        return obj;
      }
      i++; // skip ,
    }
    return obj;
  }

  function parseArray() {
    var arr = [];
    i++;
    skipWhitespace();
    if (str[i] === "]") {
      i++;
      return arr;
    }
    while (i < str.length) {
      arr.push(parseValue());
      skipWhitespace();
      if (str[i] === "]") {
        i++;
        return arr;
      }
      i++; // skip ,
    }
    return arr;
  }

  function parseString() {
    i++;
    var s = "";
    while (i < str.length) {
      var c = str[i];
      if (c === '"') {
        i++;
        return s;
      }
      if (c === "\\") {
        i++;
        var e = str[i];
        if (e === '"') s += '"';
        else if (e === "\\") s += "\\";
        else if (e === "/") s += "/";
        else if (e === "n") s += "\n";
        else if (e === "r") s += "\r";
        else if (e === "t") s += "\t";
        else if (e === "b") s += "\b";
        else if (e === "f") s += "\f";
        else if (e === "u") {
          var hex = str.substr(i + 1, 4);
          s += String.fromCharCode(parseInt(hex, 16));
          i += 4;
        }
      } else {
        s += c;
      }
      i++;
    }
    return s;
  }

  function parseNumber() {
    var start = i;
    if (str[i] === "-") i++;
    while (i < str.length && /[0-9]/.test(str[i])) i++;
    if (str[i] === ".") {
      i++;
      while (i < str.length && /[0-9]/.test(str[i])) i++;
    }
    if (str[i] === "e" || str[i] === "E") {
      i++;
      if (str[i] === "+" || str[i] === "-") i++;
      while (i < str.length && /[0-9]/.test(str[i])) i++;
    }
    return parseFloat(str.substring(start, i));
  }

  return parseValue();
}

// ── Entry point ───────────────────────────────────────────────────────────────

function main() {
  var manifestFile = File.openDialog(
    "Select the manifest.json exported by QGIS",
    "JSON files:*.json",
    false,
  );
  if (!manifestFile) {
    alert("Cancelled.");
    return;
  }

  manifestFile.encoding = "UTF-8";
  manifestFile.open("r");
  var raw = manifestFile.read();
  manifestFile.close();

  var manifest;
  try {
    manifest = parseJSON(raw);
  } catch (e) {
    alert("Could not parse manifest.json:\n" + e.message);
    return;
  }

  var pdfFolder = manifestFile.parent;
  var docW = DOC_WIDTH_PT;
  // Use the exact page dimensions QGIS used — stored in manifest.
  // Fallback to canvas pixel ratio for old manifests.
  var pageWmm = manifest.page_width_mm || manifest.canvas_width_px || 400;
  var pageHmm = manifest.page_height_mm || manifest.canvas_height_px || 300;
  var docH = DOC_WIDTH_PT * (pageHmm / pageWmm);

  // Native PDF page width in pt — used by placePDF to detect a wrong sticky
  // "Crop to" Place option. Only meaningful when the manifest has real page
  // dimensions (not the pixel fallback).
  gExpectedWpt = manifest.page_width_mm
    ? (manifest.page_width_mm * 72) / 25.4
    : 0;

  var docPreset = new DocumentPreset();
  docPreset.width = docW;
  docPreset.height = docH;
  docPreset.units = RulerUnits.Points;
  docPreset.colorMode = DocumentColorSpace.RGB;
  docPreset.title = manifest.name || "QGIS Export";

  var doc = app.documents.addDocument(DocumentColorSpace.RGB, docPreset);
  doc.rulerUnits = RulerUnits.Points;
  doc.rulerOrigin = [0, 0];

  try {
    doc.layers[0].remove();
  } catch (e) {}

  gPlaced = 0;
  gMissing = [];
  gFailed = [];
  gAbort = false;
  gSwatches = 0;
  gSwatchSkipped = [];
  gSwatchNames = {};
  gSwatchGroups = {};
  for (var s = 0; s < doc.swatches.length; s++) {
    gSwatchNames["k" + doc.swatches[s].name] = true;
  }

  var children = manifest.children || [];
  var didTranspose = false;
  if (THEME_FIRST) {
    var regrouped = maybeTransposeThemeFirst(children);
    didTranspose = regrouped !== children;
    children = regrouped;
  }

  buildLayers(doc, doc, children, pdfFolder, docW, docH, []);

  if (gAbort) {
    alert(
      "Import ABORTED after a page-size check failure.\n\n" +
        "Close this document WITHOUT saving, then fix the Place option:\n" +
        "File > Place any layer PDF with 'Show Import Options' checked,\n" +
        "set Crop to 'Media Box', place it, undo — and rerun this script.",
    );
    return;
  }

  var msg = "Import complete.\n\nPDFs placed: " + gPlaced + "\n";
  msg +=
    "Top-level layers: " +
    children.length +
    (didTranspose ? " (theme-first)" : " (as exported)");
  if (BUILD_SWATCHES && !PLACE_AS_LINKED) {
    msg += "\nGlobal swatches: " + gSwatches;
  }
  msg += summarize("Missing PDFs", gMissing);
  msg += summarize("Place failures", gFailed);
  msg += summarize("Layers without swatches", gSwatchSkipped);
  alert(msg);
}

function summarize(label, names) {
  if (!names.length) return "";
  var shown = names.slice(0, 10).join("\n  ");
  var extra =
    names.length > 10 ? "\n  … +" + (names.length - 10) + " more" : "";
  return "\n\n" + label + " (" + names.length + "):\n  " + shown + extra;
}


// ── Theme-first transposition ─────────────────────────────────────────────────
// Turns the commune-first manifest tree (root → commune groups → themes) into
// a purely thematic one: every commune's subtree is merged into a single
// shared theme tree, matching nodes by exact name at every depth. No commune
// (or organisational group holding communes) survives as a layer:
//
//   Voirie
//     Autoroute        ← one layer, one placed PDF per commune
//     Route imp. 1 …
//
// Commune groups may sit at any depth (e.g. CTMs in Lyon: root → CTM →
// communes); they merge into the same themes. Output nodes have the manifest
// shape, except merged leaf layers carry `files` instead of `file`.

function isCommuneGroup(node) {
  return node.type === "group" && (node.insee || node._fdpCommune)
    ? true
    : false;
}

// True when a (non-commune) group holds a commune group somewhere below it.
// Never descends into commune groups themselves.
function containsCommune(node) {
  var stack = [node];
  while (stack.length) {
    var kids = stack.pop().children || [];
    for (var i = 0; i < kids.length; i++) {
      if (kids[i].type !== "group") continue;
      if (isCommuneGroup(kids[i])) return true;
      stack.push(kids[i]);
    }
  }
  return false;
}

// Index of the n-th (1-based) node in `list` with this type and name, or -1.
function findNth(list, type, name, n) {
  for (var i = 0; i < list.length; i++) {
    if (list[i].type === type && list[i].name === name && --n === 0) return i;
  }
  return -1;
}

// Merges one commune's sibling list `src` into the shared theme tree `dst`,
// recursively, matching nodes by type + name. The k-th same-named sibling in
// `src` matches the k-th in `dst`, so same-named siblings WITHIN a commune are
// never collapsed together. A node seen for the first time is inserted right
// after the previously matched one, which keeps legend order even when later
// communes add themes in the middle. Leaf layers accumulate one file per
// commune in `files` (src = commune name, for error messages).
function mergeInto(dst, src, srcName) {
  var seen = {};
  var last = -1;
  for (var i = 0; i < src.length; i++) {
    var node = src[i];
    var key = node.type + "|" + node.name;
    seen[key] = (seen[key] || 0) + 1;
    var idx = findNth(dst, node.type, node.name, seen[key]);
    if (idx < 0) {
      idx = last + 1;
      dst.splice(
        idx,
        0,
        node.type === "group"
          ? { type: "group", name: node.name, children: [] }
          : { type: node.type, name: node.name, files: [] },
      );
    }
    last = idx;
    var target = dst[idx];
    if (node.type === "group") {
      mergeInto(target.children, node.children || [], srcName);
    } else if (node.file) {
      target.files.push({ file: node.file, src: srcName });
    }
  }
}

function maybeTransposeThemeFirst(children) {
  var anyInsee = false;
  var groupNames = [];

  for (var i = 0; i < children.length; i++) {
    if (children[i].type !== "group") continue;
    groupNames.push(children[i].name);
    if (isCommuneGroup(children[i]) || containsCommune(children[i])) {
      anyInsee = true;
    }
  }

  if (anyInsee) {
    // New-style manifest (exporter v6): commune groups are tagged, at
    // whatever depth. Regroup exactly those; everything else stays put.
    return transposeThemeFirst(children);
  }

  if (!groupNames.length) return children;

  // Old manifest without commune tags: only the user knows whether the
  // top-level groups are communes. NEVER regroup silently — merging
  // same-named sibling groups (e.g. an Axo per-building stack) destroys
  // their draw order.
  var listed = groupNames.slice(0, 15).join("\n  ");
  if (groupNames.length > 15) {
    listed += "\n  … +" + (groupNames.length - 15) + " more";
  }
  var ok = confirm(
    "Theme-first regroup?\n\n" +
      "Treat every top-level group as a commune and merge their\n" +
      "sub-layers into shared theme master layers?\n\n  " +
      listed +
      "\n\nYes = regroup by theme (multi-commune FDP imports)\n" +
      "No = import the tree exactly as exported (safe default)",
  );
  if (!ok) return children;

  for (var f = 0; f < children.length; f++) {
    if (children[f].type === "group") children[f]._fdpCommune = true;
  }
  return transposeThemeFirst(children);
}

function transposeThemeFirst(rootChildren) {
  var masters = [];
  var res = transposeLevel(rootChildren, masters);
  if (!masters.length) return rootChildren;

  // The masters block replaces the commune groups at the position of the
  // first one; every non-commune node keeps its place.
  var out = [];
  for (var i = 0; i < res.anchor; i++) out.push(res.rest[i]);
  for (var m = 0; m < masters.length; m++) out.push(masters[m]);
  for (var j = res.anchor; j < res.rest.length; j++) out.push(res.rest[j]);
  return out;
}

// One sibling list → { rest, anchor }, merging the content of every commune
// group found in it (directly, or nested inside organisational groups) into
// the shared `masters` theme tree:
//   rest     every other node, original order;
//   anchor   index in `rest` where the masters block belongs.
function transposeLevel(children, masters) {
  var rest = [];
  var anchor = -1;

  for (var i = 0; i < children.length; i++) {
    var node = children[i];

    if (isCommuneGroup(node)) {
      if (anchor < 0) anchor = rest.length;
      mergeInto(masters, node.children || [], node.name);
    } else if (node.type === "group" && containsCommune(node)) {
      // Organisational group (CTM, lot…): its communes merge straight into
      // the themes — the organisational level disappears with them.
      if (anchor < 0) anchor = rest.length;
      var inner = transposeLevel(node.children || [], masters);
      // Anything in the group that isn't commune content stays in a group
      // of the same name, right after the masters block.
      if (inner.rest.length) {
        rest.push({ type: "group", name: node.name, children: inner.rest });
      }
    } else {
      rest.push(node);
    }
  }

  if (anchor < 0) anchor = rest.length;
  return { rest: rest, anchor: anchor };
}

// ── Layer tree builder ────────────────────────────────────────────────────────

// `path` = names of the ancestor groups, root first (path[0] = theme).
function buildLayers(doc, parent, children, pdfFolder, docW, docH, path) {
  // Illustrator's layers.add() always inserts at index 0 (top of stack).
  // To get manifest order (index 0 = top layer) we iterate in REVERSE —
  // the last manifest entry is added first (sits at bottom), and the first
  // manifest entry is added last (lands on top). No zOrder calls needed.
  for (var i = children.length - 1; i >= 0; i--) {
    if (gAbort) return;
    var node = children[i];

    if (node.type === "group") {
      var grpLayer = addLayer(doc, parent, node.name);
      if (node.children && node.children.length) {
        buildLayers(
          doc,
          grpLayer,
          node.children,
          pdfFolder,
          docW,
          docH,
          path.concat([node.name]),
        );
      }
    } else if (node.type === "layer") {
      var aiLayer = addLayer(doc, parent, node.name);
      // Merged theme layers carry one file per commune in `files`; nodes
      // imported as exported carry a single `file`.
      var files = node.files || (node.file ? [{ file: node.file }] : []);
      var missing = 0;
      var failed = 0;
      var placedBefore = gPlaced;

      for (var f = 0; f < files.length; f++) {
        var label = files[f].src
          ? node.name + " (" + files[f].src + ")"
          : node.name;
        var pdfFile = new File(pdfFolder.fsName + "/" + files[f].file);
        if (!pdfFile.exists) {
          gMissing.push(label);
          missing++;
          continue;
        }
        if (!placePDF(doc, aiLayer, pdfFile, docW, docH, label)) failed++;
        if (gAbort) return;
      }

      // Swatches need the art editable, i.e. embedded. Done per layer once
      // all communes are placed, so one swatch drives the whole drawing.
      if (BUILD_SWATCHES && !PLACE_AS_LINKED && gPlaced > placedBefore) {
        try {
          buildSwatches(doc, aiLayer, path, node.name);
        } catch (e) {
          gSwatchSkipped.push(node.name + " — " + e.message);
        }
      }

      if (missing) aiLayer.name += " [PDF missing]";
      if (failed) aiLayer.name += " [place failed]";
    }
  }
}

// ── Layer helpers ─────────────────────────────────────────────────────────────

function addLayer(doc, parent, name) {
  var layer = parent === doc ? doc.layers.add() : parent.layers.add();
  layer.name = name || "unnamed";
  return layer;
}

// ── Global swatches ───────────────────────────────────────────────────────────
// Colours are read from the embedded art itself — that IS the QGIS
// appearance — so there is nothing to match against the QGIS renderer and
// nothing that can drift. Each distinct colour in a leaf layer becomes one
// global process swatch (editing it recolours every commune at once), filed
// in a swatch group named after the theme; every path using that colour is
// linked to it. Anything that isn't a plain RGB/gray fill or stroke
// (patterns, gradients, rasters, clipping paths) is left untouched.

var ROLE_LABELS = { fill: "fond", stroke: "trait" };

function buildSwatches(doc, aiLayer, path, name) {
  // 1. Collect distinct colours (first-seen order) and the paths using them.
  var colours = [];
  var byKey = {};

  function use(item, role, color) {
    var rgb = rgbOf(color);
    if (!rgb) return;
    var key = "k" + rgb.join(",");
    if (!byKey[key]) {
      byKey[key] = { rgb: rgb, role: role, uses: [] };
      colours.push(byKey[key]);
    }
    byKey[key].uses.push({ item: item, role: role });
  }

  function visitPath(p) {
    if (p.clipping) return;
    if (p.filled) use(p, "fill", p.fillColor);
    if (p.stroked) use(p, "stroke", p.strokeColor);
  }

  var stack = [aiLayer];
  while (stack.length) {
    var items = stack.pop().pageItems;
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      if (it.typename === "GroupItem") {
        stack.push(it);
      } else if (it.typename === "CompoundPathItem") {
        for (var j = 0; j < it.pathItems.length; j++) visitPath(it.pathItems[j]);
      } else if (it.typename === "PathItem") {
        visitPath(it);
      }
    }
  }

  if (!colours.length) return;
  if (colours.length > MAX_SWATCHES_PER_LAYER) {
    gSwatchSkipped.push(name + " — " + colours.length + " colours");
    return;
  }

  // 2. One global swatch per colour, named after the layer (+ role when the
  //    layer has several colours), then relink every path to it.
  var group = swatchGroupFor(doc, path.length ? path[0] : name);
  var parentName = path.length ? path[path.length - 1] : "";
  var roleCount = {};

  for (var c = 0; c < colours.length; c++) {
    var col = colours[c];
    var label = name;
    if (colours.length > 1) {
      var role = ROLE_LABELS[col.role];
      roleCount[role] = (roleCount[role] || 0) + 1;
      label += " · " + role + (roleCount[role] > 1 ? " " + roleCount[role] : "");
    }

    var spot = doc.spots.add();
    spot.color = makeRGB(col.rgb);
    spot.colorType = ColorModel.PROCESS; // global process colour, not a spot ink
    spot.name = uniqueSwatchName(label, parentName);
    group.addSpot(spot);
    gSwatches++;

    var sc = new SpotColor();
    sc.spot = spot;
    sc.tint = 100;
    for (var u = 0; u < col.uses.length; u++) {
      if (col.uses[u].role === "fill") col.uses[u].item.fillColor = sc;
      else col.uses[u].item.strokeColor = sc;
    }
  }
}

// [r, g, b] rounded to integers, or null for anything that isn't RGB/gray.
function rgbOf(color) {
  if (color.typename === "RGBColor") {
    return [
      Math.round(color.red),
      Math.round(color.green),
      Math.round(color.blue),
    ];
  }
  if (color.typename === "GrayColor") {
    var v = Math.round(255 * (1 - color.gray / 100)); // gray = % of black
    return [v, v, v];
  }
  return null;
}

function makeRGB(rgb) {
  var c = new RGBColor();
  c.red = rgb[0];
  c.green = rgb[1];
  c.blue = rgb[2];
  return c;
}

// Swatch names must be unique document-wide: the layer name if free, else
// prefixed with its parent group, else numbered.
function uniqueSwatchName(label, parentName) {
  var name = label;
  if (gSwatchNames["k" + name] && parentName) name = parentName + " / " + label;
  var base = name;
  for (var n = 2; gSwatchNames["k" + name]; n++) name = base + " (" + n + ")";
  gSwatchNames["k" + name] = true;
  return name;
}

function swatchGroupFor(doc, name) {
  var group = gSwatchGroups["k" + name];
  if (!group) {
    group = doc.swatchGroups.add();
    group.name = name;
    gSwatchGroups["k" + name] = group;
  }
  return group;
}

// ── PDF placement ─────────────────────────────────────────────────────────────

// Returns false when placement failed or the import was aborted.
function placePDF(doc, aiLayer, pdfFile, docW, docH, label) {
  var item;
  try {
    item = aiLayer.placedItems.add();
    item.file = pdfFile;
  } catch (e) {
    gFailed.push(label + ": " + e.message);
    return false;
  }

  // Guard against Illustrator's sticky "Crop to" Place option: anything but
  // "Media Box" (e.g. "Bounding Box" = crop to artwork) crops and scales each
  // PDF differently, so the layers fall out of register. Native page width
  // must match the manifest before we scale.
  if (gExpectedWpt > 0) {
    var nativeW = 0;
    try {
      nativeW = item.width;
    } catch (e) {}
    if (nativeW > 0 && Math.abs(nativeW - gExpectedWpt) > 2) {
      // No "continue anyway": a wrong crop can only produce an
      // out-of-register document. Abort and explain.
      alert(
        "Page-size check failed on '" +
          label +
          "':\nexpected " +
          Math.round(gExpectedWpt * 10) / 10 +
          " pt wide, got " +
          Math.round(nativeW * 10) / 10 +
          " pt.\n\n" +
          "Illustrator's sticky Place option 'Crop to' is not 'Media Box'\n" +
          "(e.g. 'Bounding Box' crops each PDF to its artwork), so layers\n" +
          "cannot be placed in register. Import will abort.",
      );
      gAbort = true;
      return false;
    }
  }

  // 1. Scale uniformly to fit artboard width exactly.
  //    All PDFs are the same page size so this is consistent across layers.
  // 2. Position top-left at artboard top-left ([0, docH] in Illustrator's
  //    Y-up coordinate system).
  // Canvas rotation is already baked into the PDFs by qgis_export_layers.py
  // (setMapRotation) — do NOT rotate again here.
  try {
    var curW = item.width;
    var curH = item.height;
    if (curW > 0 && curH > 0) {
      var scale = (docW / curW) * 100;
      item.resize(scale, scale, true, true, true, true, scale);
    }
    item.position = [0, docH];
  } catch (e) {}

  // Embed AFTER sizing, positioning and rotation.
  if (!PLACE_AS_LINKED) {
    try {
      item.embed();
    } catch (e) {}
  }

  gPlaced++;
  if (gPlaced % 10 === 0) app.redraw();
  return true;
}

// ── Run ───────────────────────────────────────────────────────────────────────

try {
  main();
} catch (e) {
  alert("Script error:\n" + e.message + "\nLine: " + e.line);
}
