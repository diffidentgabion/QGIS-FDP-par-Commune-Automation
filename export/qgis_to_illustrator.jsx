/**
 * QGIS → Illustrator Layer Importer  v6.4
 * qgis_to_illustrator.jsx
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
var THEME_FIRST = true; // true  = one master layer per theme, commune sublayers inside
//                         false = original commune-first mirror of the QGIS tree

// ── Globals ───────────────────────────────────────────────────────────────────

var gPlaced = 0;
var gMissing = []; // layer names whose PDF file was absent
var gFailed = []; // layer names where placement threw
var gExpectedWpt = 0; // native PDF page width in pt; 0 disables the size check
var gAbort = false; // set when the user aborts on a size-check failure

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

  var children = manifest.children || [];
  var didTranspose = false;
  if (THEME_FIRST) {
    var regrouped = maybeTransposeThemeFirst(children);
    didTranspose = regrouped !== children;
    children = regrouped;
  }

  buildLayers(doc, doc, children, pdfFolder, docW, docH);

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
  msg += summarize("Missing PDFs", gMissing);
  msg += summarize("Place failures", gFailed);
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
// theme-first (root → theme masters → commune sublayers), so each theme is a
// single master layer shared by all communes. Themes are matched across
// communes by exact name. Output nodes have the same shape as manifest nodes,
// so buildLayers consumes the result unchanged.
//
// Commune groups may sit at any depth: when the user files them into
// organisational groups in QGIS (e.g. CTMs in Lyon: root → CTM → communes),
// the organisational level is kept INSIDE each theme master:
//
//   Voirie
//     CTM Nord
//       Caluire-et-Cuire
//       Rillieux-la-Pape
//     CTM Est …

function cloneNodeRenamed(node, newName) {
  var copy = {};
  for (var k in node) {
    if (node.hasOwnProperty(k)) copy[k] = node[k];
  }
  copy.name = newName;
  return copy;
}

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
  var res = transposeLevel(rootChildren);
  if (!res.masters.length) return rootChildren;

  // The masters block replaces the commune groups at the position of the
  // first one; every non-commune node keeps its place.
  var out = [];
  for (var i = 0; i < res.anchor; i++) out.push(res.rest[i]);
  for (var m = 0; m < res.masters.length; m++) out.push(res.masters[m]);
  for (var j = res.anchor; j < res.rest.length; j++) out.push(res.rest[j]);
  return out;
}

// One sibling list → { masters, rest, anchor }:
//   masters  theme groups built from the commune groups found in this list
//            (directly, or nested inside organisational groups);
//   rest     every other node, original order;
//   anchor   index in `rest` where the masters block belongs.
// Master order = first contributor's theme order (legend order); themes
// first seen later are appended.
function transposeLevel(children) {
  var masters = [];
  var rest = [];
  var anchor = -1;

  function masterFor(name) {
    for (var m = 0; m < masters.length; m++) {
      if (masters[m].name === name) return masters[m];
    }
    var master = { type: "group", name: name, children: [] };
    masters.push(master);
    return master;
  }

  for (var i = 0; i < children.length; i++) {
    var node = children[i];

    if (isCommuneGroup(node)) {
      if (anchor < 0) anchor = rest.length;
      var themes = node.children || [];
      for (var t = 0; t < themes.length; t++) {
        var theme = themes[t];
        if (theme.type === "group") {
          masterFor(theme.name).children.push({
            type: "group",
            name: node.name,
            children: theme.children || [],
          });
        } else {
          // Leaf theme (single PDF): the commune sublayer carries the file.
          masterFor(theme.name).children.push(
            cloneNodeRenamed(theme, node.name),
          );
        }
      }
    } else if (node.type === "group" && containsCommune(node)) {
      // Organisational group (CTM, lot…): regroup its communes, then wrap
      // each resulting theme under a sublayer named after the group.
      if (anchor < 0) anchor = rest.length;
      var inner = transposeLevel(node.children || []);
      for (var k = 0; k < inner.masters.length; k++) {
        masterFor(inner.masters[k].name).children.push({
          type: "group",
          name: node.name,
          children: inner.masters[k].children,
        });
      }
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
  return { masters: masters, rest: rest, anchor: anchor };
}

// ── Layer tree builder ────────────────────────────────────────────────────────

function buildLayers(doc, parent, children, pdfFolder, docW, docH) {
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
        buildLayers(doc, grpLayer, node.children, pdfFolder, docW, docH);
      }
    } else if (node.type === "layer") {
      var aiLayer = addLayer(doc, parent, node.name);
      if (!node.file) continue;

      var pdfFile = new File(pdfFolder.fsName + "/" + node.file);
      if (!pdfFile.exists) {
        gMissing.push(node.name);
        aiLayer.name = node.name + " [PDF missing]";
        continue;
      }

      placePDF(doc, aiLayer, pdfFile, docW, docH);
    }
  }
}

// ── Layer helpers ─────────────────────────────────────────────────────────────

function addLayer(doc, parent, name) {
  var layer = parent === doc ? doc.layers.add() : parent.layers.add();
  layer.name = name || "unnamed";
  return layer;
}

// ── PDF placement ─────────────────────────────────────────────────────────────

function placePDF(doc, aiLayer, pdfFile, docW, docH) {
  var item;
  try {
    item = aiLayer.placedItems.add();
    item.file = pdfFile;
  } catch (e) {
    gFailed.push(aiLayer.name);
    aiLayer.name = aiLayer.name + " [place failed: " + e.message + "]";
    return;
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
          aiLayer.name +
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
      return;
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
}

// ── Run ───────────────────────────────────────────────────────────────────────

try {
  main();
} catch (e) {
  alert("Script error:\n" + e.message + "\nLine: " + e.line);
}
