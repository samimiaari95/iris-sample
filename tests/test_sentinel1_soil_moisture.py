from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import numpy as np
from affine import Affine
from pyproj import CRS

from app.satellite.soil_moisture import (
    Acquisition,
    BackscatterUnits,
    RasterGrid,
    analyze,
    run_config,
)
from scripts.create_sentinel1_demo import create_demo


def scene(
    scene_id: str,
    day: int,
    vv: list[list[float]],
    vh: list[list[float]],
    units: BackscatterUnits = "db",
) -> Acquisition:
    return Acquisition(
        scene_id=scene_id,
        acquired_at=datetime(2025, 2, day, 6, tzinfo=timezone.utc),
        relative_orbit=73,
        vv=np.asarray(vv, dtype=np.float64),
        vh=np.asarray(vh, dtype=np.float64),
        units=units,
    )


class SoilMoistureAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.grid = RasterGrid(
            width=2,
            height=2,
            transform=Affine(20, 0, 350000, 0, -20, 5650000),
            crs=CRS.from_epsg(25832),
        )
        self.baseline_vv = [[-10.0, -12.0], [-8.0, -11.0]]
        self.baseline_vh = [[-18.0, -20.0], [-16.0, -19.0]]

    def test_db_conversion_dual_pol_candidate_and_nodata_coverage(self) -> None:
        acquisitions = [
            scene("baseline-a", 1, self.baseline_vv, self.baseline_vh),
            scene("baseline-b", 13, self.baseline_vv, self.baseline_vh),
            scene(
                "current",
                25,
                [[-8.0, -10.0], [-6.0, -11.0]],
                [[-16.0, -20.0], [-9999.0, -19.0]],
            ),
        ]
        collection, report = analyze(
            acquisitions,
            self.grid,
            tile_id="tile-1",
            min_baseline_observations=2,
            source={"imagery_is_synthetic": True},
        )

        self.assertEqual(len(collection["features"]), 3)
        candidate = next(
            feature
            for feature in collection["features"]
            if feature["properties"]["row"] == 0
            and feature["properties"]["column"] == 0
        )
        self.assertAlmostEqual(candidate["properties"]["vv_anomaly_db"], 2.0)
        self.assertAlmostEqual(candidate["properties"]["vh_anomaly_db"], 2.0)
        self.assertTrue(candidate["properties"]["wetness_candidate"])
        vv_only_change = next(
            feature
            for feature in collection["features"]
            if feature["properties"]["row"] == 0
            and feature["properties"]["column"] == 1
        )
        self.assertFalse(vv_only_change["properties"]["wetness_candidate"])
        self.assertEqual(candidate["id"], "s1wet:tile-1:20250225:000000:000000")
        self.assertEqual(report["valid_pixels"], 3)
        self.assertEqual(report["rejected_pixels"], 1)
        self.assertEqual(report["wetness_candidate_pixels"], 1)
        self.assertEqual(report["invalid_vh_samples"], 1)
        self.assertEqual(
            report["rejected_pixel_reason_counts"]["missing_current_vh"],
            1,
        )
        self.assertEqual(report["input_crs"], "EPSG:25832")
        self.assertEqual(candidate["geometry"]["type"], "Polygon")

    def test_linear_and_db_inputs_produce_equivalent_anomalies(self) -> None:
        db_scenes = [
            scene("baseline-a", 1, [[-10]], [[-18]]),
            scene("baseline-b", 13, [[-10]], [[-18]]),
            scene("current", 25, [[-8]], [[-16]]),
        ]
        linear_scenes = [
            scene(
                acquisition.scene_id,
                acquisition.acquired_at.day,
                (10 ** (np.asarray(acquisition.vv) / 10)).tolist(),
                (10 ** (np.asarray(acquisition.vh) / 10)).tolist(),
                units=cast(BackscatterUnits, "linear"),
            )
            for acquisition in db_scenes
        ]
        single_pixel_grid = RasterGrid(
            width=1,
            height=1,
            transform=Affine(20, 0, 350000, 0, -20, 5650000),
            crs=CRS.from_epsg(25832),
        )
        db_result, _ = analyze(
            db_scenes,
            single_pixel_grid,
            tile_id="equivalent",
            min_baseline_observations=2,
        )
        linear_result, _ = analyze(
            linear_scenes,
            single_pixel_grid,
            tile_id="equivalent",
            min_baseline_observations=2,
        )
        for polarization in ("vv", "vh"):
            self.assertAlmostEqual(
                db_result["features"][0]["properties"][f"{polarization}_anomaly_db"],
                linear_result["features"][0]["properties"][f"{polarization}_anomaly_db"],
            )

    def test_temporal_median_resists_one_speckle_outlier(self) -> None:
        acquisitions = [
            scene("baseline-a", 1, [[-10]], [[-18]]),
            scene("baseline-b", 7, [[-10]], [[-18]]),
            scene("speckle-outlier", 13, [[0]], [[-8]]),
            scene("current", 25, [[-9]], [[-17]]),
        ]
        single_pixel_grid = RasterGrid(
            width=1,
            height=1,
            transform=Affine(20, 0, 350000, 0, -20, 5650000),
            crs=CRS.from_epsg(25832),
        )

        collection, _ = analyze(
            acquisitions,
            single_pixel_grid,
            tile_id="speckle-test",
        )

        self.assertAlmostEqual(
            collection["features"][0]["properties"]["vv_anomaly_db"],
            1.0,
        )
        self.assertAlmostEqual(
            collection["features"][0]["properties"]["vh_anomaly_db"],
            1.0,
        )

    def test_zero_linear_power_and_nonfinite_samples_are_rejected(self) -> None:
        acquisitions = [
            scene("baseline-a", 1, [[0.1, 0.1]], [[0.02, 0.02]], units="linear"),
            scene("baseline-b", 13, [[0.1, 0.1]], [[0.02, 0.02]], units="linear"),
            scene(
                "current",
                25,
                [[0.0, 0.1]],
                [[0.02, np.nan]],
                units="linear",
            ),
        ]
        grid = RasterGrid(
            width=2,
            height=1,
            transform=Affine(20, 0, 350000, 0, -20, 5650000),
            crs=CRS.from_epsg(25832),
        )

        collection, report = analyze(
            acquisitions,
            grid,
            tile_id="invalid-test",
            min_baseline_observations=2,
        )

        self.assertEqual(collection["features"], [])
        self.assertEqual(report["status"], "no_valid_pixels")
        self.assertEqual(report["rejected_pixels"], 2)
        self.assertEqual(report["invalid_vv_samples"], 1)
        self.assertEqual(report["invalid_vh_samples"], 1)

    def test_rejects_misaligned_arrays_and_mixed_relative_orbits(self) -> None:
        good = scene("good", 1, [[-10]], [[-18]])
        misaligned = scene("misaligned", 13, [[-10, -10]], [[-18, -18]])
        current = scene("current", 25, [[-8]], [[-16]])
        single_pixel_grid = RasterGrid(
            width=1,
            height=1,
            transform=Affine(20, 0, 350000, 0, -20, 5650000),
            crs=CRS.from_epsg(25832),
        )
        with self.assertRaisesRegex(ValueError, "shape"):
            analyze(
                [good, misaligned, current],
                single_pixel_grid,
                tile_id="test",
                min_baseline_observations=2,
            )

        different_orbit = Acquisition(
            scene_id="different-orbit",
            acquired_at=datetime(2025, 2, 13, 6, tzinfo=timezone.utc),
            relative_orbit=74,
            vv=np.asarray([[-10.0]]),
            vh=np.asarray([[-18.0]]),
            units="db",
        )
        with self.assertRaisesRegex(ValueError, "relative orbit"):
            analyze(
                [good, different_orbit, current],
                single_pixel_grid,
                tile_id="test",
                min_baseline_observations=2,
            )

    def test_requires_enough_baseline_observations(self) -> None:
        with self.assertRaisesRegex(ValueError, "needs a current acquisition"):
            analyze(
                [scene("baseline", 1, [[-10]], [[-18]])],
                RasterGrid(
                    width=1,
                    height=1,
                    transform=Affine(20, 0, 350000, 0, -20, 5650000),
                    crs=CRS.from_epsg(25832),
                ),
                tile_id="test",
                min_baseline_observations=2,
            )

    def test_rejects_non_metric_grid(self) -> None:
        with self.assertRaisesRegex(ValueError, "EPSG:25832"):
            RasterGrid(
                width=1,
                height=1,
                transform=Affine(0.01, 0, 7, 0, -0.01, 51),
                crs=CRS.from_epsg(4326),
            )

    def test_reads_synthetic_geotiffs_and_writes_quality_report(self) -> None:
        with TemporaryDirectory() as temp_directory:
            config_path = create_demo(Path(temp_directory))
            output_path, report_path = run_config(config_path)
            collection = json.loads(output_path.read_text(encoding="utf-8"))
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(len(collection["features"]), 3)
        self.assertEqual(report["valid_pixels"], 3)
        self.assertEqual(report["rejected_pixels"], 1)
        self.assertEqual(report["wetness_candidate_pixels"], 1)
        self.assertEqual(len(report["source_files"]), 4)


if __name__ == "__main__":
    unittest.main()
