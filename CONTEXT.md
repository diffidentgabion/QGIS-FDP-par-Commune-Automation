**Project context:**
I am building a QGIS processing toolbox script in Python (PyQGIS) for an architecture and urbanism office. The tool automates base map creation for French communes. I am a novice coder so please explain your key decisions as comments in the code. Target environment is QGIS 3.28 or later. You are a skilled and helpful PyQGIS programmer experienced in interfacing with French GIS data. You build extremely fast, high-performance tools, and your deep knowledge of architecture/urbanism workflows lets you develop streamlined solutions for use in offices.

**What the tool does:**
The user runs the script from the QGIS Processing Toolbox. They type a commune name (partial name search supported). The script fetches all data from online sources, builds a styled layer group, and offers to save a .qgz project file locally.

**Step-by-step logic to implement:**

1. **Commune search** — Query `https://geo.api.gouv.fr/communes?nom={input}&fields=nom,code,contour&format=geojson&geometry=contour` with the user's input. If multiple results are returned, show a selection dialog listing commune name and department code so the user can pick the right one. Store the commune GeoJSON boundary and its INSEE code for use in all subsequent steps.

2. **Projection** — All layers must be loaded and clipped in EPSG:2154 (Lambert 93). Reproject on the fly if needed.

3. **IGN Géoplateforme WFS layers** — Load the following layers via WFS from `https://data.geopf.fr/wfs/ows`. Apply a bounding box filter derived from the commune boundary on every WFS request to keep queries fast. Clip each layer to the commune boundary polygon after loading. Load layers in this order (bottom to top):
   - Administrative boundaries: `ADMINEXPRESS-COG-CARTO.LATEST:commune`
   - Cadastral parcels: `CADASTRALPARCELS.PARCELLAIRE_EXPRESS:parcelle`
   - Hydrography: `BDTOPO_V3:cours_d_eau` and `BDTOPO_V3:surface_hydrographique`
   - Transport: `BDTOPO_V3:troncon_de_route` and `BDTOPO_V3:voie_ferree`
   - Bâti: `BDTOPO_V3:batiment`
   - Végétation: `BDTOPO_V3:zone_de_vegetation`

4. **SIRENE establishments** — Download the current Géo-SIRENE établissements file from `https://files.data.gouv.fr/geo-sirene/last/dep/geo_siret_{dep}.csv.gz` where `{dep}` is the two-digit department code derived from the INSEE code. Filter rows by the commune INSEE code column (`codecommuneetablissement`). Load the filtered result as a point layer clipped to the commune boundary.

5. **Layer naming and grouping** — Place all layers inside a QGIS layer group named after the commune. Use clear, human-readable French layer names (e.g. "Parcelles cadastrales", "Bâti", "Hydrographie - cours d'eau").

6. **Default symbology** — Apply sensible default styling to each layer. Use a coherent, minimal color palette appropriate for an architectural base map: light/neutral tones for most layers, with subtle differentiation between layer types. Parcels should have no fill and a thin dark outline. Buildings should be a medium grey fill. Roads should be white or light grey lines of varying width by type if road type attributes are available. Vegetation should be a light green. Water should be light blue. SIRENE points should be small dark dots.

7. **Progress feedback** — Log clear progress messages to the QGIS log panel at each step (e.g. "Fetching commune boundary…", "Loading cadastral parcels…") so the user knows the script is working.

8. **Save option** — When all layers are loaded, show a dialog asking the user if they want to save the project as a .qgz file. If yes, open a file save dialog defaulting to a filename like `{commune_name}_basemap.qgz`.

**Implementation notes:**
- Handle errors gracefully: if a WFS layer returns no features, log a warning and continue rather than crashing.
- The Géo-SIRENE file is large; download it to a temporary directory and delete it after filtering and loading.
- Do not hardcode the commune name anywhere; everything should derive from the user's input and the API responses.
- Write the script as a single .py file structured as a QGIS Processing script (using `QgsProcessingAlgorithm`).

**Before writing any code**, please write out your full implementation plan describing how you will approach each step, which libraries you will use, and any assumptions you are making. Wait for my approval before proceeding.