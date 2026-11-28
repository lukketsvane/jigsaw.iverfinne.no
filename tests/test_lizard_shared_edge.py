from collections import defaultdict

import numpy as np
from shapely.geometry import box
from shapely.ops import unary_union

from pathnet.lizard_edge_eval import component_count
from pathnet.lizard_edge_model import edit_tile_edges
from pathnet.lizard_net import make_warp
from pathnet.lizard_tile import LizardTile, make_tiles


def test_learned_seam_is_reversed_exactly_at_the_wrap():
    units, tiles = make_tiles(2, 2)
    warp = make_warp(777, *units, piece_count=len(tiles))
    scale = np.array([18.0, 17.8])
    periods = np.diag(units) * scale
    physical = []
    owners = defaultdict(list)
    for pid, tile in enumerate(tiles):
        edges = tuple(warp(edge) * scale for edge in tile.edges)
        physical.append(LizardTile(np.vstack([e[:-1] for e in edges]), edges,
                                   warp(tile.spine) * scale, warp(tile.eyes) * scale))
        for side, edge in enumerate(edges):
            ticks = np.rint(edge / periods * 1e6).astype(np.int64) % 1_000_000
            owners[min(ticks.tobytes(), ticks[::-1].tobytes())].append((pid, side))

    result = edit_tile_edges(physical, periods)
    displacement = max(np.max(np.abs(a.outline - b.outline)) for a, b in zip(physical, result))
    assert displacement > 0.1
    wrapped_axes = np.zeros(2, dtype=bool)
    for ((i, s), (j, t)) in owners.values():
        a, b = result[i].edges[s], result[j].edges[t][::-1]
        shift = np.rint((a[0] - b[0]) / periods)
        wrapped_axes |= shift != 0
        np.testing.assert_allclose(a, b + shift * periods, rtol=0, atol=1e-7)
        # Lifting the opposite owner by a float-valued period introduces roundoff.
        np.testing.assert_allclose(a[[0, -1]], physical[i].edges[s][[0, -1]], rtol=0, atol=1e-10)
    assert wrapped_axes.all()


def test_three_millimeter_neck_is_stricter_than_the_kerf_test():
    piece = unary_union([box(0, 0, 8, 8), box(8, 3, 12, 5), box(12, 0, 20, 8)])
    assert component_count(piece, 0.125) == 1
    assert component_count(piece, 1.5) == 2
    assert component_count(box(0, 0, 1, 1), 1.5) == 0

