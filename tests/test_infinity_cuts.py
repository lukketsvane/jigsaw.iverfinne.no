import base64
import json
import xml.etree.ElementTree as ET
from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from dendrite_engine.infinity import _icosahedron, generate
from dendrite_engine.infinity_print import (
    _planar_uv,
    _projective_uv,
    _sphere_points,
    _sphere_uv,
)


@pytest.mark.parametrize(
    "surface,n,chi",
    [("torus", 12, 0), ("klein", 12, 0), ("projective", 12, 1), ("sphere", 80, 2)],
)
def test_actual_cut_paths_pair_on_closed_surface(tmp_path, surface, n, chi):
    result = generate(
        tmp_path,
        surface,
        seed=993,
        cols=4,
        rows=3,
        frequency=2,
        cell_mm=15,
        pixels_per_mm=1,
    )
    assert result["verified_svg_pieces"] == n
    assert result["euler_characteristic"] == chi
    assert result["topological_vertices"] - result["surface_edges"] + n == chi
    cuts = [
        np.fromstring(e.attrib["d"][1:].replace(",", " "), sep=" ").reshape(-1, 2)
        for e in ET.parse(tmp_path / "cut.svg").getroot().findall(".//{*}path")
    ]
    sides = []
    duplicated = []
    for pair in result["joins"]:
        sides.extend(map(tuple, pair["sides"]))
        a, b = (cuts[k] for k in pair["cut_ids"])
        transform = np.array(pair["map_first_to_second"])
        assert np.linalg.det(transform[:, :2]) == pytest.approx(
            -1 if pair["flip"] else 1, abs=1e-8
        )
        actual = a @ transform[:, :2].T + transform[:, 2]
        # An interior cut is stored once in the SVG, in its first piece's direction.
        expected = b if pair["same_cut"] or pair["flip"] else b[::-1]
        assert np.max(np.linalg.norm(actual - expected, axis=1)) < 0.00022
        if not pair["same_cut"]:
            duplicated.append(pair)
    assert len(sides) == len(set(sides)) == (3 if surface == "sphere" else 4) * n
    assert any(p["flip"] for p in duplicated) == (surface in ("klein", "projective"))
    if surface == "sphere":
        assert any(abs(p["map_first_to_second"][0][1]) > 0.1 for p in duplicated)
    for file in result["print_files"]:
        picture = ET.parse(tmp_path / file).getroot()
        assert (
            picture.attrib["viewBox"]
            == ET.parse(tmp_path / "cut.svg").getroot().attrib["viewBox"]
        )
        png = base64.b64decode(picture.find("{*}image").attrib["href"].split(",", 1)[1])
        with Image.open(BytesIO(png)) as im:
            assert im.mode == "RGBA" and im.getchannel("A").getextrema() == (0, 255)


def test_spherical_projection_recovers_area_weights_and_crosses_cut_edges():
    _cells, charts, xyz = _icosahedron(1, 32)
    ids, corners = charts[0]
    weights = np.array([[0.19, 0.34, 0.47], [0.8, 0.05, 0.15], [1, 0, 0]])
    triangle = xyz[list(ids)]
    p = _sphere_points(weights, triangle)

    def area(a, b, c):
        return 2 * np.arctan2(np.dot(a, np.cross(b, c)), 1 + a @ b + b @ c + c @ a)

    for point, w in zip(p, weights):
        recovered = [
            area(point, triangle[(i + 1) % 3], triangle[(i + 2) % 3]) / area(*triangle)
            for i in range(3)
        ]
        assert recovered == pytest.approx(w, abs=1e-9)
    # Sample just past a north cap edge. The wrapped neighboring chart is far
    # away on the net. Its independent barycentric lookup must yield the same pixel.
    weights_across = np.array([[-0.08, 0.51, 0.57]])
    shared = set(ids[1:])
    mate = next(
        i for i, (other, _) in enumerate(charts) if i != 0 and shared <= set(other)
    )
    mate_ids, mate_xy = charts[mate]
    by_id = {ids[1]: 0.43, ids[2]: 0.49, (set(mate_ids) - shared).pop(): 0.08}
    p1 = weights_across @ corners
    p2 = np.array([[by_id[v] for v in mate_ids]]) @ mate_xy
    assert np.allclose(
        _sphere_uv(p1, 0, charts, xyz), _sphere_uv(p2, mate, charts, xyz)
    )


def test_klein_double_cover_and_retry(tmp_path):
    width, height = 60, 45
    a = np.array([[width, 18], [width, 33]], dtype=float)
    b = np.column_stack((a[:, 0] - width, height - a[:, 1]))
    assert np.allclose(
        _planar_uv(a, (width, height), True, False),
        _planar_uv(b, (width, height), True, True),
    )
    assert np.allclose(
        _projective_uv(a, (width, height), False),
        _projective_uv(b, (width, height), True),
    )
    bottom = np.array([[18, height], [21, height]])
    top = np.column_stack((width - bottom[:, 0], bottom[:, 1] - height))
    assert np.allclose(
        _projective_uv(bottom, (width, height), False),
        _projective_uv(top, (width, height), True),
    )
    art = tmp_path / "photo.png"
    Image.fromarray(np.arange(96, dtype=np.uint8).reshape(4, 8, 3)).save(art)
    out = tmp_path / "job"
    generate(out, "klein", cols=4, rows=3, cell_mm=15, artwork=art, pixels_per_mm=1)
    before = (out / "cut.svg").read_bytes()
    with pytest.raises(FileNotFoundError):
        generate(out, "sphere", artwork=tmp_path / "missing.png")
    assert (out / "cut.svg").read_bytes() == before
    assert (out / "artwork-back.svg").exists()
    generate(out, "sphere", frequency=1, cell_mm=15, artwork=art, pixels_per_mm=1)
    assert not (out / "artwork-back.svg").exists()
    assert json.loads((out / "assembly.json").read_text())["verified_svg_pieces"] == 20


def test_sphere_orientation_avoids_land_mask(tmp_path):
    from PIL import Image
    import numpy as np
    from dendrite_engine.infinity import _icosahedron, generate

    _, _, vertices = _icosahedron(1, 12)
    u = (np.arctan2(vertices[:, 1], vertices[:, 0]) / (2 * np.pi) + 0.5) % 1
    v = 0.5 - np.arcsin(vertices[:, 2]) / np.pi
    mask = np.zeros((90, 180), np.uint8)
    mask[np.clip((90 * v).astype(int), 0, 89), (u * 180).astype(int)] = 255
    source = tmp_path / "land.png"
    Image.fromarray(mask).save(source)
    run = generate(
        tmp_path / "earth", "sphere", frequency=1, pixels_per_mm=0.5, land_mask=source
    )
    result = run["map_orientation"]
    assert result["original_vertices_on_land"] >= 10
    assert result["selected_vertices_on_land"] == 0
    assert run["verified_svg_pieces"] == 20
