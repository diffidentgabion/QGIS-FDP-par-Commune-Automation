I'm building a QGIS Processing Toolbox script (`fdp_par_commune.py`) that loads BD TOPO and SIRENE layers for French communes, each into a named layer group. The script can be run multiple times to load multiple communes into the same project, each producing an identical layer structure inside its own group.

I want to add a **thematic view system** that lets the user toggle layer visibility by theme (e.g. "Éducation", "Voirie", "Bâti intrinsèque") across all loaded communes simultaneously, and that continues to work correctly as new communes are added later.

**The implementation must be highly modular with respect to theme definitions.** Themes should be defined in a single data structure (e.g. a dict or list of dataclasses) that maps a theme name to a list of layer name patterns, so that adding a new theme in the future requires only adding an entry to that structure and nothing else. The pattern matching should be flexible enough to handle both exact layer names and partial matches (e.g. all "Bâti — Éducation" sublayers across gradient steps).

**Before writing any code**, please answer the following:

1. QGIS map themes are static snapshots of the current layer tree state. If I auto-generate a theme called "Éducation" when loading commune A, and then load commune B which adds new layers, does the theme automatically include the new layers or is it frozen at snapshot time? Is there any native QGIS mechanism for dynamic cross-group visibility?

2. Is it possible to attach a persistent signal or observer in PyQGIS that fires when layers are added to the project, which could be used to update theme definitions or apply visibility rules dynamically?

3. Given the layer naming convention is consistent across communes (e.g. every commune group contains a layer called "Bâti — Éducation"), is programmatic visibility toggling by layer name pattern a viable and performant approach for 3-5 communes with ~20-30 layers each?

4. What is the lightest-weight persistent UI element in PyQGIS for housing a set of theme toggle buttons or checkboxes that survives script re-runs within the same QGIS session — dock widget, toolbar, or something else?

5. Given the answers above, what is your recommended architecture for this system?

Prefer the architecturally correct solution over a simpler but fragile one — I'd rather understand and implement something properly than patch around its limitations later.
