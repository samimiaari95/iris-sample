"""Sentinel-1 backscatter anomaly workflow for a soil-wetness candidate indicator."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import rasterio
from affine import Affine
from numpy.typing import NDArray
from pyproj import CRS, Transformer
from shapely import transform as transform_geometry
from shapely.geometry import Polygon, mapping

BackscatterUnits = Literal["linear", "db"]
FloatRaster = NDArray[np.float64]
OUTPUT_CRS = CRS.from_epsg(4326)
INPUT_EPSG = 25832


@dataclass(frozen=True)
class RasterGrid:
    width: int
    height: int
    transform: Affine
    crs: CRS

    def __post_init__(self) -> None:
        if (
            not isinstance(self.width, int)
            or isinstance(self.width, bool)
            or not isinstance(self.height, int)
            or isinstance(self.height, bool)
        ):
            raise ValueError("Raster width and height must be integers.")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Raster width and height must be positive.")
        if not isinstance(self.transform, Affine):
            raise ValueError("Raster transform must be an affine transform.")
        if not isinstance(self.crs, CRS):
            raise ValueError("Raster CRS must be a pyproj CRS.")
        if self.crs.to_epsg() != INPUT_EPSG:
            raise ValueError("Input rasters must use metric CRS EPSG:25832.")
        pixel_width = math.hypot(self.transform.a, self.transform.d)
        pixel_height = math.hypot(self.transform.b, self.transform.e)
        if not math.isfinite(pixel_width) or not math.isfinite(pixel_height):
            raise ValueError("Raster pixel dimensions must be finite.")
        if pixel_width <= 0 or pixel_height <= 0:
            raise ValueError("Raster pixel dimensions must be positive.")


@dataclass(frozen=True)
class Acquisition:
    scene_id: str
    acquired_at: datetime
    relative_orbit: int
    vv: FloatRaster
    vh: FloatRaster
    units: BackscatterUnits
    product_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scene_id, str) or not self.scene_id.strip():
            raise ValueError("scene_id must be a non-empty string.")
        if self.product_id is not None and (
            not isinstance(self.product_id, str) or not self.product_id.strip()
        ):
            raise ValueError("product_id must be a non-empty string when supplied.")
        if not isinstance(self.acquired_at, datetime):
            raise ValueError("acquired_at must be a datetime.")
        if self.acquired_at.tzinfo is None:
            raise ValueError("acquired_at must include a timezone.")
        if (
            not isinstance(self.relative_orbit, int)
            or isinstance(self.relative_orbit, bool)
            or not 1 <= self.relative_orbit <= 175
        ):
            raise ValueError("relative_orbit must be an integer between 1 and 175.")
        if self.units not in ("linear", "db"):
            raise ValueError("units must be either 'linear' or 'db'.")


def _to_linear_power(values: FloatRaster, units: BackscatterUnits) -> tuple[FloatRaster, int]:
    """Return sigma0 power in linear units and the number of rejected samples."""
    if units not in ("linear", "db"):
        raise ValueError(f"Unsupported backscatter units: {units!r}.")

    raw = np.asarray(values, dtype=np.float64)
    valid = np.isfinite(raw)
    if units == "linear":
        valid &= raw > 0
        power = np.full(raw.shape, np.nan, dtype=np.float64)
        power[valid] = raw[valid]
    else:
        power = np.full(raw.shape, np.nan, dtype=np.float64)
        with np.errstate(over="ignore", under="ignore", invalid="ignore"):
            power[valid] = np.power(10.0, raw[valid] / 10.0)
        valid &= np.isfinite(power) & (power > 0)
        power[~valid] = np.nan

    return power, int(raw.size - np.count_nonzero(valid))


def _temporal_median(stack: list[FloatRaster]) -> tuple[FloatRaster, NDArray[np.int64]]:
    values = np.stack(stack, axis=0)
    counts = np.sum(np.isfinite(values), axis=0, dtype=np.int64)
    median = np.ma.median(np.ma.masked_invalid(values), axis=0).filled(np.nan)
    return np.asarray(median, dtype=np.float64), counts


def analyze(
    acquisitions: list[Acquisition],
    grid: RasterGrid,
    *,
    tile_id: str,
    threshold_db: float = 1.0,
    min_baseline_observations: int = 3,
    source: dict[str, Any] | None = None,
    source_files: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compare the latest acquisition with earlier, same-orbit per-pixel medians."""
    if not isinstance(tile_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", tile_id):
        raise ValueError("tile_id must contain only letters, digits, '.', '_' or '-'.")
    if (
        isinstance(threshold_db, bool)
        or not isinstance(threshold_db, (int, float))
        or not math.isfinite(threshold_db)
        or threshold_db <= 0
    ):
        raise ValueError("threshold_db must be a finite value greater than zero.")
    if (
        not isinstance(min_baseline_observations, int)
        or isinstance(min_baseline_observations, bool)
        or min_baseline_observations < 1
    ):
        raise ValueError("min_baseline_observations must be at least one.")
    if source is not None and not isinstance(source, dict):
        raise ValueError("source metadata must be a JSON object.")
    if len(acquisitions) < min_baseline_observations + 1:
        raise ValueError(
            "The workflow needs a current acquisition plus at least "
            f"{min_baseline_observations} baseline acquisitions."
        )
    if len({item.scene_id for item in acquisitions}) != len(acquisitions):
        raise ValueError("Acquisition scene_id values must be unique.")

    ordered = sorted(acquisitions, key=lambda item: item.acquired_at)
    if any(item.acquired_at.tzinfo is None for item in ordered):
        raise ValueError("Acquisition timestamps must include a timezone.")
    if len({item.acquired_at for item in ordered}) != len(ordered):
        raise ValueError("Acquisition timestamps must be unique.")
    if len({item.relative_orbit for item in ordered}) != 1:
        raise ValueError("All acquisitions must share the same Sentinel-1 relative orbit.")
    if any(not 1 <= item.relative_orbit <= 175 for item in ordered):
        raise ValueError("Sentinel-1 relative_orbit must be between 1 and 175.")

    expected_shape = (grid.height, grid.width)
    vv_linear: list[FloatRaster] = []
    vh_linear: list[FloatRaster] = []
    invalid_vv = 0
    invalid_vh = 0
    for acquisition in ordered:
        vv = np.asarray(acquisition.vv, dtype=np.float64)
        vh = np.asarray(acquisition.vh, dtype=np.float64)
        if vv.shape != expected_shape or vh.shape != expected_shape:
            raise ValueError(
                f"Scene {acquisition.scene_id!r} arrays must have shape {expected_shape}."
            )
        vv_values, vv_rejected = _to_linear_power(vv, acquisition.units)
        vh_values, vh_rejected = _to_linear_power(vh, acquisition.units)
        vv_linear.append(vv_values)
        vh_linear.append(vh_values)
        invalid_vv += vv_rejected
        invalid_vh += vh_rejected

    baseline_scenes = ordered[:-1]
    current = ordered[-1]
    baseline_vv, vv_baseline_counts = _temporal_median(vv_linear[:-1])
    baseline_vh, vh_baseline_counts = _temporal_median(vh_linear[:-1])
    current_vv = vv_linear[-1]
    current_vh = vh_linear[-1]

    with np.errstate(divide="ignore", invalid="ignore"):
        vv_anomaly = 10.0 * np.log10(current_vv / baseline_vv)
        vh_anomaly = 10.0 * np.log10(current_vh / baseline_vh)
    valid = (
        np.isfinite(vv_anomaly)
        & np.isfinite(vh_anomaly)
        & (vv_baseline_counts >= min_baseline_observations)
        & (vh_baseline_counts >= min_baseline_observations)
    )
    candidates = valid & (vv_anomaly >= threshold_db) & (vh_anomaly >= threshold_db)

    to_wgs84 = Transformer.from_crs(
        CRS.from_epsg(INPUT_EPSG),
        OUTPUT_CRS,
        always_xy=True,
    ).transform
    features: list[dict[str, Any]] = []
    for row, col in zip(*np.nonzero(valid), strict=True):
        top_left = grid.transform @ (int(col), int(row))
        top_right = grid.transform @ (int(col) + 1, int(row))
        bottom_right = grid.transform @ (int(col) + 1, int(row) + 1)
        bottom_left = grid.transform @ (int(col), int(row) + 1)
        metric_polygon = Polygon((top_left, top_right, bottom_right, bottom_left))
        if not metric_polygon.is_valid or metric_polygon.area <= 0:
            raise ValueError(f"Invalid raster cell geometry at row={row}, col={col}.")

        def reproject_coordinates(
            coordinates: NDArray[np.float64],
        ) -> NDArray[np.float64]:
            longitude, latitude = to_wgs84(coordinates[:, 0], coordinates[:, 1])
            return np.column_stack((longitude, latitude))

        geographic_polygon = transform_geometry(
            metric_polygon,
            reproject_coordinates,
        )
        if not geographic_polygon.is_valid:
            raise ValueError(f"Invalid output geometry at row={row}, col={col}.")

        feature_id = (
            f"s1wet:{tile_id}:{current.acquired_at:%Y%m%d}:"
            f"{int(row):06d}:{int(col):06d}"
        )
        features.append(
            {
                "type": "Feature",
                "id": feature_id,
                "geometry": mapping(geographic_polygon),
                "properties": {
                    "tile_id": tile_id,
                    "scene_id": current.scene_id,
                    "product_id": current.product_id or current.scene_id,
                    "acquired_at": current.acquired_at.isoformat(),
                    "relative_orbit": current.relative_orbit,
                    "row": int(row),
                    "column": int(col),
                    "input_crs": f"EPSG:{INPUT_EPSG}",
                    "vv_anomaly_db": float(vv_anomaly[row, col]),
                    "vh_anomaly_db": float(vh_anomaly[row, col]),
                    "threshold_db": float(threshold_db),
                    "wetness_candidate": bool(candidates[row, col]),
                    "baseline_scene_count": len(baseline_scenes),
                    "vv_baseline_observation_count": int(vv_baseline_counts[row, col]),
                    "vh_baseline_observation_count": int(vh_baseline_counts[row, col]),
                },
            }
        )

    total_pixels = grid.width * grid.height
    valid_pixels = int(np.count_nonzero(valid))
    report = {
        "indicator": "sentinel1_backscatter_wetness_candidate",
        "tile_id": tile_id,
        "status": "ok" if valid_pixels else "no_valid_pixels",
        "input_crs": f"EPSG:{INPUT_EPSG}",
        "output_crs": "OGC:CRS84 (GeoJSON longitude, latitude)",
        "current_scene_id": current.scene_id,
        "current_product_id": current.product_id or current.scene_id,
        "current_acquired_at": current.acquired_at.isoformat(),
        "relative_orbit": current.relative_orbit,
        "baseline_scene_ids": [item.scene_id for item in baseline_scenes],
        "baseline_start": baseline_scenes[0].acquired_at.isoformat(),
        "baseline_end": baseline_scenes[-1].acquired_at.isoformat(),
        "baseline_scene_count": len(baseline_scenes),
        "minimum_baseline_observations_per_pixel": min_baseline_observations,
        "backscatter_input_units": ordered[-1].units,
        "speckle_treatment": (
            "per-pixel temporal median in linear sigma0 power; no spatial filter"
        ),
        "threshold_db_per_polarization": threshold_db,
        "decision_rule": (
            "candidate when both VV and VH anomalies meet or exceed the threshold"
        ),
        "total_pixels": total_pixels,
        "valid_pixels": valid_pixels,
        "rejected_pixels": total_pixels - valid_pixels,
        "coverage_fraction": valid_pixels / total_pixels,
        "wetness_candidate_pixels": int(np.count_nonzero(candidates)),
        "rejected_pixel_reason_counts": {
            "missing_current_vv": int(np.count_nonzero(~np.isfinite(current_vv))),
            "missing_current_vh": int(np.count_nonzero(~np.isfinite(current_vh))),
            "insufficient_vv_baseline": int(
                np.count_nonzero(vv_baseline_counts < min_baseline_observations)
            ),
            "insufficient_vh_baseline": int(
                np.count_nonzero(vh_baseline_counts < min_baseline_observations)
            ),
        },
        "invalid_vv_samples": invalid_vv,
        "invalid_vh_samples": invalid_vh,
        "source": source or {},
        "source_files": source_files or [],
    }
    return {"type": "FeatureCollection", "features": features}, report


def _parse_timestamp(value: Any, scene_id: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Scene {scene_id!r} acquired_at must be an ISO-8601 string.")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Scene {scene_id!r} has an invalid acquired_at timestamp.") from error
    if timestamp.tzinfo is None:
        raise ValueError(f"Scene {scene_id!r} acquired_at must include a timezone.")
    return timestamp


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_scene(
    scene: dict[str, Any],
    *,
    config_directory: Path,
    units: BackscatterUnits,
    band_indices: dict[str, int],
    expected_grid: RasterGrid | None,
) -> tuple[Acquisition, RasterGrid, dict[str, str]]:
    scene_id = scene.get("scene_id")
    if not isinstance(scene_id, str) or not scene_id.strip():
        raise ValueError("Each scene must have a non-empty scene_id.")
    relative_path = scene.get("path")
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError(f"Scene {scene_id!r} must have a raster path.")
    raster_path = (config_directory / relative_path).resolve()
    orbit = scene.get("relative_orbit")
    if not isinstance(orbit, int) or isinstance(orbit, bool):
        raise ValueError(f"Scene {scene_id!r} relative_orbit must be an integer.")

    with rasterio.open(raster_path) as dataset:
        if dataset.crs is None or dataset.crs.to_epsg() != INPUT_EPSG:
            raise ValueError(f"Scene {scene_id!r} raster must use EPSG:{INPUT_EPSG}.")
        if max(band_indices.values()) > dataset.count or min(band_indices.values()) < 1:
            raise ValueError(
                f"Scene {scene_id!r} does not contain configured VV/VH raster bands."
            )
        grid = RasterGrid(
            width=dataset.width,
            height=dataset.height,
            transform=dataset.transform,
            crs=CRS.from_user_input(dataset.crs),
        )
        if expected_grid is not None and (
            grid.width != expected_grid.width
            or grid.height != expected_grid.height
            or grid.transform != expected_grid.transform
            or grid.crs != expected_grid.crs
        ):
            raise ValueError(
                f"Scene {scene_id!r} does not align exactly with the other input rasters."
            )

        vv_masked = dataset.read(band_indices["vv"], masked=True).astype(np.float64)
        vh_masked = dataset.read(band_indices["vh"], masked=True).astype(np.float64)
        vv = np.ma.filled(vv_masked, np.nan)
        vh = np.ma.filled(vh_masked, np.nan)

    acquisition = Acquisition(
        scene_id=scene_id,
        acquired_at=_parse_timestamp(scene.get("acquired_at"), scene_id),
        relative_orbit=orbit,
        vv=np.asarray(vv, dtype=np.float64),
        vh=np.asarray(vh, dtype=np.float64),
        units=units,
        product_id=scene.get("product_id", scene_id),
    )
    source_file = {
        "scene_id": scene_id,
        "file_name": raster_path.name,
        "sha256": _sha256(raster_path),
        "product_id": str(scene.get("product_id", scene_id)),
    }
    return acquisition, grid, source_file


def run_config(config_path: Path, output_override: Path | None = None) -> tuple[Path, Path]:
    config_path = config_path.resolve()
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes)
    if not isinstance(config, dict):
        raise ValueError("The job configuration must contain a JSON object.")

    unit_value = config.get("backscatter_units")
    if unit_value not in ("linear", "db"):
        raise ValueError("backscatter_units must be either 'linear' or 'db'.")
    band_indices = config.get("band_indices", {"vv": 1, "vh": 2})
    if (
        not isinstance(band_indices, dict)
        or set(band_indices) != {"vv", "vh"}
        or any(
            not isinstance(band_indices.get(polarization), int)
            or isinstance(band_indices.get(polarization), bool)
            or band_indices.get(polarization) < 1
            for polarization in ("vv", "vh")
        )
        or band_indices["vv"] == band_indices["vh"]
    ):
        raise ValueError(
            "band_indices must define distinct positive integer 'vv' and 'vh' bands."
        )
    scenes = config.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("scenes must be a non-empty list.")
    source = config.get("source", {})
    if not isinstance(source, dict):
        raise ValueError("source must be a JSON object.")

    acquisitions: list[Acquisition] = []
    source_files: list[dict[str, str]] = []
    grid: RasterGrid | None = None
    for scene in scenes:
        if not isinstance(scene, dict):
            raise ValueError("Each scenes entry must be a JSON object.")
        acquisition, scene_grid, source_file = _read_scene(
            scene,
            config_directory=config_path.parent,
            units=unit_value,
            band_indices=band_indices,
            expected_grid=grid,
        )
        grid = grid or scene_grid
        acquisitions.append(acquisition)
        source_files.append(source_file)

    if grid is None:
        raise ValueError("No readable scene rasters were provided.")
    collection, report = analyze(
        acquisitions,
        grid,
        tile_id=config.get("tile_id", ""),
        threshold_db=config.get("threshold_db", 1.0),
        min_baseline_observations=config.get("min_baseline_observations", 3),
        source=source,
        source_files=source_files,
    )
    report["config_sha256"] = hashlib.sha256(config_bytes).hexdigest()
    report["input_band_indices"] = dict(band_indices)

    if output_override is not None:
        output_path = output_override.resolve()
    else:
        output_value = config.get("output_file", "soil_moisture.geojson")
        if not isinstance(output_value, str) or not output_value:
            raise ValueError("output_file must be a non-empty path string.")
        output_path = config_path.parent / output_value
    output_path = output_path.resolve()
    report_path = output_path.with_name(f"{output_path.stem}_quality_report.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(collection, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return output_path, report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a Sentinel-1 VV/VH backscatter anomaly GeoJSON."
    )
    parser.add_argument("--config", required=True, type=Path, help="Path to job JSON.")
    parser.add_argument("--output", type=Path, help="Override the configured GeoJSON path.")
    args = parser.parse_args(argv)
    try:
        output_path, report_path = run_config(args.config, args.output)
    except (OSError, ValueError, json.JSONDecodeError, rasterio.errors.RasterioError) as error:
        parser.error(str(error))
    print(f"GeoJSON: {output_path}")
    print(f"Quality report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
