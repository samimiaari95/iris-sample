"""Generate deterministic synthetic GeoTIFF inputs for the Sentinel-1 demo."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin


NODATA = -9999.0
SCENES = (
    {
        "scene_id": "SYNTHETIC-S1-20250201",
        "acquired_at": "2025-02-01T06:00:00Z",
        "vv": [[-10.0, -12.0], [-8.0, -11.0]],
        "vh": [[-18.0, -20.0], [-16.0, -19.0]],
    },
    {
        "scene_id": "SYNTHETIC-S1-20250213",
        "acquired_at": "2025-02-13T06:00:00Z",
        "vv": [[-10.0, -12.0], [-8.0, -11.0]],
        "vh": [[-18.0, -20.0], [-16.0, -19.0]],
    },
    {
        "scene_id": "SYNTHETIC-S1-20250225",
        "acquired_at": "2025-02-25T06:00:00Z",
        "vv": [[-10.0, -12.0], [-8.0, -11.0]],
        "vh": [[-18.0, -20.0], [-16.0, -19.0]],
    },
    {
        "scene_id": "SYNTHETIC-S1-20250309",
        "acquired_at": "2025-03-09T06:00:00Z",
        "vv": [[-8.0, -10.0], [-6.0, -11.0]],
        "vh": [[-16.0, -20.0], [NODATA, -19.0]],
    },
)


def create_demo(output_directory: Path) -> Path:
    output_directory.mkdir(parents=True, exist_ok=True)
    for scene in SCENES:
        output_path = output_directory / f"{scene['scene_id']}.tif"
        profile = {
            "driver": "GTiff",
            "width": 2,
            "height": 2,
            "count": 2,
            "dtype": "float32",
            "crs": "EPSG:25832",
            "transform": from_origin(350000, 5650000, 20, 20),
            "nodata": NODATA,
            "compress": "deflate",
        }
        with rasterio.open(output_path, "w", **profile) as dataset:
            dataset.write(np.asarray(scene["vv"], dtype=np.float32), 1)
            dataset.write(np.asarray(scene["vh"], dtype=np.float32), 2)
            dataset.set_band_description(1, "VV")
            dataset.set_band_description(2, "VH")
            dataset.update_tags(
                source="synthetic fixture; not Sentinel-1 imagery",
                scene_id=scene["scene_id"],
            )

    config = {
        "tile_id": "demo-25832",
        "backscatter_units": "db",
        "band_indices": {"vv": 1, "vh": 2},
        "threshold_db": 1.0,
        "min_baseline_observations": 3,
        "output_file": "sentinel1_demo_result.geojson",
        "source": {
            "name": "Synthetic Sentinel-1-like fixture",
            "url": "https://dataspace.copernicus.eu/data-collections/copernicus-sentinel-missions/sentinel-1",
            "product_type": "synthetic dual-polarization sigma0 GeoTIFF",
            "license": "Synthetic data; no Sentinel-1 product or license is represented.",
            "imagery_is_synthetic": True,
            "imagery_selection_note": (
                "Synthetic data used instead of live imagery because time and "
                "resource constraints prevented obtaining and preparing a real scene."
            ),
            "resolution_m": 20,
            "masking": "One VH no-data pixel uses GeoTIFF nodata -9999.",
        },
        "scenes": [
            {
                "scene_id": scene["scene_id"],
                "product_id": scene["scene_id"],
                "acquired_at": scene["acquired_at"],
                "relative_orbit": 73,
                "path": f"{scene['scene_id']}.tif",
            }
            for scene in SCENES
        ],
    }
    config_path = output_directory / "config.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/sentinel1_demo"),
        help="Directory for generated rasters and config (default: data/sentinel1_demo).",
    )
    args = parser.parse_args()
    print(create_demo(args.output_dir.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
