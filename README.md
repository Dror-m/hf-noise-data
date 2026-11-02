# hf-noise-data

Pre-tiled OpenStreetMap data of man-made HF (1.5–30 MHz) noise sources for Israel and the Palestinian territories, used by the HF noise map app.

A GitHub Actions workflow (`.github/workflows/build.yml`) runs monthly (and on demand from the Actions tab):

1. downloads the Geofabrik extract `israel-and-palestine-latest.osm.pbf`;
2. keeps only noise-source features (power lines and substations, solar, landuse areas, railways, main roads, masts, water works, greenhouses, electric fences, etc.) and national border lines;
3. writes them as 0.2° tiles (`tiles/`) and 1° border tiles (`borders/`) with an `index.json`, published on GitHub Pages.

Data © OpenStreetMap contributors, available under the Open Database License (ODbL).
