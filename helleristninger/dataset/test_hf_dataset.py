"""Automated validation tests for Nordic Helleristninger Hugging Face dataset and Explorer bundle."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

DATASET_DIR = Path(__file__).resolve().parent
BASE_DIR = DATASET_DIR.parent
EXPLORER_DIR = BASE_DIR / "explorer"

VALID_DATING_METHODS = {
    "radiocarbon_14c",
    "shoreline_displacement",
    "stratigraphic",
    "typological_stylistic",
    "composite_archival",
    "unknown",
}


def _assert_no_nan(val: Any, context: str) -> None:
    if isinstance(val, float):
        assert not math.isnan(val) and not math.isinf(val), f"NaN/Inf float at {context}"
    elif isinstance(val, str):
        assert val.strip().lower() not in ("nan", "inf", "-inf"), f"String NaN/Inf at {context}: {val!r}"


def _validate_geojson_file(path: Path) -> int:
    assert path.is_file(), f"Missing GeoJSON: {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("type") == "FeatureCollection"
    feats = data.get("features")
    assert isinstance(feats, list) and len(feats) > 0
    for idx, feat in enumerate(feats):
        assert feat.get("type") == "Feature", f"Invalid feature at {idx}"
        geom = feat.get("geometry")
        if geom is not None:
            assert geom.get("type") == "Point"
            coords = geom.get("coordinates")
            assert isinstance(coords, list) and len(coords) == 2
            lon, lat = coords
            assert isinstance(lon, (int, float)) and isinstance(lat, (int, float))
            assert not math.isnan(lon) and not math.isnan(lat)
            assert -180.0 <= float(lon) <= 180.0, f"Invalid lon at {idx}: {lon}"
            assert -90.0 <= float(lat) <= 90.0, f"Invalid lat at {idx}: {lat}"
        props = feat.get("properties")
        assert isinstance(props, dict)
        for k, v in props.items():
            _assert_no_nan(v, f"{path.name}[{idx}].{k}")
    return len(feats)


def test_parquet_files_readable_and_clean() -> None:
    required_parquets = [
        DATASET_DIR / "figures.parquet",
        DATASET_DIR / "sites.parquet",
        DATASET_DIR / "sources.parquet",
        DATASET_DIR / "dual_trace_qa.parquet",
        DATASET_DIR / "splits" / "figures_commercial_safe.parquet",
        DATASET_DIR / "splits" / "figures_research_only.parquet",
    ]
    for pq_path in required_parquets:
        assert pq_path.is_file(), f"Missing Parquet file: {pq_path}"
        table = pq.read_table(pq_path)
        assert table.num_rows >= 0
        for col_name in table.column_names:
            col = table[col_name].to_pylist()
            for row_i, val in enumerate(col[:500]):
                _assert_no_nan(val, f"{pq_path.name}:{col_name}[{row_i}]")


def test_license_split_integrity() -> None:
    all_tbl = pq.read_table(DATASET_DIR / "figures.parquet", columns=["figure_id", "commercial_ok"])
    comm_tbl = pq.read_table(DATASET_DIR / "splits" / "figures_commercial_safe.parquet", columns=["figure_id", "commercial_ok"])
    res_tbl = pq.read_table(DATASET_DIR / "splits" / "figures_research_only.parquet", columns=["figure_id", "commercial_ok"])

    all_ids = set(all_tbl["figure_id"].to_pylist())
    comm_ids = set(comm_tbl["figure_id"].to_pylist())
    res_ids = set(res_tbl["figure_id"].to_pylist())

    assert len(all_ids) == all_tbl.num_rows
    assert comm_ids.isdisjoint(res_ids)
    assert (comm_ids | res_ids) == all_ids
    assert all(comm_tbl["commercial_ok"].to_pylist())
    assert not any(res_tbl["commercial_ok"].to_pylist())


def test_geojson_validity_and_dating_invariants() -> None:
    fig_geo_n = _validate_geojson_file(DATASET_DIR / "figures.geojson")
    site_geo_n = _validate_geojson_file(DATASET_DIR / "sites.geojson")

    fig_tbl = pq.read_table(
        DATASET_DIR / "figures.parquet",
        columns=["figure_id", "date_from_year", "date_to_year", "date_midpoint_year", "dating_method", "confidence"],
    )
    site_tbl = pq.read_table(
        DATASET_DIR / "sites.parquet",
        columns=["site_uid", "date_from_year", "date_to_year", "dating_method"],
    )
    assert fig_geo_n == fig_tbl.num_rows
    assert site_geo_n == site_tbl.num_rows

    for df, dt, dm, conf in zip(
        fig_tbl["date_from_year"].to_pylist(),
        fig_tbl["date_to_year"].to_pylist(),
        fig_tbl["dating_method"].to_pylist(),
        fig_tbl["confidence"].to_pylist(),
    ):
        assert dm in VALID_DATING_METHODS
        assert 0.0 <= float(conf) <= 1.0
        if df is not None and dt is not None:
            assert df <= dt

    for df, dt, dm in zip(
        site_tbl["date_from_year"].to_pylist(),
        site_tbl["date_to_year"].to_pylist(),
        site_tbl["dating_method"].to_pylist(),
    ):
        assert dm in VALID_DATING_METHODS
        if df is not None and dt is not None:
            assert df <= dt


def test_csv_jsonl_readme_and_stats_consistency() -> None:
    stats_path = DATASET_DIR / "dataset_stats.json"
    readme_path = DATASET_DIR / "README.md"
    assert stats_path.is_file()
    assert readme_path.is_file()

    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    fig_tbl = pq.read_table(DATASET_DIR / "figures.parquet")
    site_tbl = pq.read_table(DATASET_DIR / "sites.parquet")
    src_tbl = pq.read_table(DATASET_DIR / "sources.parquet")

    assert stats["figures_count"] == fig_tbl.num_rows
    assert stats["sites_count"] == site_tbl.num_rows
    assert stats["sources_count"] == src_tbl.num_rows

    with open(DATASET_DIR / "figures.csv", "r", encoding="utf-8") as f:
        csv_count = sum(1 for _ in csv.DictReader(f))
    assert csv_count == fig_tbl.num_rows

    readme_text = readme_path.read_text(encoding="utf-8")
    assert readme_text.startswith("---\n")
    for cfg in ("figures", "sites", "sources", "dual_trace_qa"):
        assert f"config_name: {cfg}" in readme_text


def test_explorer_bundle_ready() -> None:
    for fname in ("index.html", "app.js", "styles.css", "explorer_data.json"):
        assert (EXPLORER_DIR / fname).is_file(), f"Missing explorer file: {fname}"
    data = json.loads((EXPLORER_DIR / "explorer_data.json").read_text(encoding="utf-8"))
    assert len(data["sites"]) == data["stats"]["sites_count"]
    assert len(data["figures"]) == data["stats"]["figures_count"]
    chunks = list((EXPLORER_DIR / "chunks").glob("svgs_*.json"))
    assert len(chunks) >= 1
