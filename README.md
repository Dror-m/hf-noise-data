# hf-noise-data

Pre-tiled OpenStreetMap data of man-made HF (1.5–30 MHz) noise sources, used by the HF noise map app. Regions to build are listed in `regions.json` (Geofabrik region paths).

A GitHub Actions workflow (`.github/workflows/build.yml`) runs monthly (and on demand from the Actions tab):

1. downloads the Geofabrik extract of every region in `regions.json` (a region can be added or removed with the workflow's `add_region` / `remove_region` inputs);
2. keeps only noise-source features (power lines and substations, solar, landuse areas, railways, main roads, masts, water works, greenhouses, electric fences, etc.) national border lines, island outlines and ground cover for ground-wave attenuation (coastlines, salt pans, lakes and reservoirs, irrigated/wet land);
3. writes each region to `r/<id>/` as 0.2° tiles (`tiles/`), 1° border and island tiles (`borders/`, `islands/`), 0.2° ground tiles (`ground/`) and an `index.json`, plus a top-level `regions.json`, published on GitHub Pages.

Data © OpenStreetMap contributors, available under the Open Database License (ODbL).
