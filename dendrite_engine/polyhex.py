"""Independent colored polyhex exact-cover puzzles on an axial hex grid.

Public rules and exact-cover description: https://n-e-r-v-o-u-s.com/blog/?p=9827
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
import random
import xml.etree.ElementTree as ET


DIRECTIONS = ((1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1))


def _normalize(shape):
    cells = tuple(shape)
    q0, r0 = min(q for q, r in cells), min(r for q, r in cells)
    return tuple(sorted((q - q0, r - r0) for q, r in cells))


@lru_cache(maxsize=None)
def orientations(shape):
    """Translations and 60-degree rotations are equivalent. Reflections are distinct."""
    result = set()
    for _ in range(6):
        shape = _normalize(shape)
        result.add(shape)
        shape = tuple((-r, q + r) for q, r in shape)
    return tuple(sorted(result))


@lru_cache(maxsize=None)
def tile_set(max_cells=5):
    if not 1 <= max_cells <= 5:
        raise ValueError("Polyhex enumeration supports sizes 1 through 5")
    current = {((0, 0),)}
    result = []
    for size in range(1, max_cells + 1):
        result.extend(sorted(current))
        if size < max_cells:
            next_size = set()
            for shape in current:
                points = set(shape)
                fringe = {(q + dq, r + dr) for q, r in shape for dq, dr in DIRECTIONS} - points
                for cell in fringe:
                    next_size.add(orientations(_normalize((*shape, cell)))[0])
            current = next_size
    return tuple(result)


def hex_board(radius=8):
    return frozenset((q, r) for q in range(-radius, radius + 1)
                     for r in range(-radius, radius + 1) if abs(q + r) <= radius)


def _rim(shape):
    cells = set(shape)
    return {(q + dq, r + dr) for q, r in cells for dq, dr in DIRECTIONS} - cells


@dataclass
class Search:
    solutions: list
    nodes: int
    exhausted: bool


def solve(board, shapes, colors, fixed=None, *, limit=2, node_limit=100_000, seed=0):
    """Count distinct completions up to limit. A budget cutoff never proves uniqueness.

    Colors are integer IDs. None for a tile disables its edge-color constraint.
    Each placement covers the identity of its tile and all its grid coordinates.
    Equal-colored neighbors are excluded when a placement is selected.
    """
    board = frozenset(board)
    shapes = tuple(shapes)
    fixed = {i: tuple(sorted(cells)) for i, cells in (fixed or {}).items()}
    if limit < 1 or node_limit < 1 or len(colors) != len(shapes):
        raise ValueError("Invalid polyhex search bounds or color count")
    occupied = {}
    for i, cells in fixed.items():
        if not 0 <= i < len(shapes) or _normalize(cells) not in orientations(shapes[i]):
            raise ValueError("Fixed placement disagrees with its tile shape")
        if not set(cells) <= board or any(c in occupied for c in cells):
            raise ValueError("Fixed tiles overlap or extend outside the board")
        if colors[i] is not None and any(colors[occupied[c]] == colors[i] for c in _rim(cells) if c in occupied):
            raise ValueError("Fixed tiles share an edge and a color")
        occupied.update(dict.fromkeys(cells, i))
    free = board - occupied.keys()
    missing = [i for i in range(len(shapes)) if i not in fixed]
    if sum(len(shapes[i]) for i in missing) != len(free):
        return Search([], 0, True)
    if not missing:
        return Search([dict(fixed)], 1, True)
    board_cells = sorted(free)
    cell_index = {p: i for i, p in enumerate(board_cells)}
    tile_index = {t: len(board_cells) + i for i, t in enumerate(missing)}
    placements = []
    # Anchor a specific cell of each orientation at every empty position. Distinct
    # orientations have already been reduced under rotational symmetry.
    for i in missing:
        allowed = free
        if colors[i] is not None:
            forbidden = _rim(c for c, j in occupied.items() if colors[j] == colors[i])
            allowed = free - forbidden
        for direction in orientations(shapes[i]):
            aq, ar = direction[0]
            offsets = [(q - aq, r - ar) for q, r in direction]
            for q, r in sorted(allowed):
                cells = tuple((q + dq, r + dr) for dq, dr in offsets)
                if all(c in allowed for c in cells):
                    placements.append((i, cells))

    random.Random(seed).shuffle(placements)
    # Largest first so the single-cell tile and domino remain available for small voids.
    placements.sort(key=lambda row: -len(row[1]))
    columns = [0] * (len(free) + len(missing))
    color_rows = defaultdict(int)
    row_columns = []
    for ri, (tile, cells) in enumerate(placements):
        bit = 1 << ri
        cols = (tile_index[tile], *(cell_index[p] for p in cells))
        row_columns.append(cols)
        for col in cols:
            columns[col] |= bit
        if colors[tile] is not None:
            color_rows[colors[tile]] |= bit

    @lru_cache(maxsize=4096)
    def conflicts(ri):
        tile, cells = placements[ri]
        excluded = 0
        for col in row_columns[ri]:
            excluded |= columns[col]
        if colors[tile] is not None:
            neighbors = 0
            for p in _rim(cells) & free:
                neighbors |= columns[cell_index[p]]
            excluded |= neighbors & color_rows[colors[tile]]
        return excluded

    solutions, chosen = [], []
    nodes, stopped = 0, False

    def visit(active, required):
        nonlocal nodes, stopped
        if nodes >= node_limit:
            stopped = True
            return
        nodes += 1
        if not required:
            solutions.append({**fixed, **dict(placements[i] for i in chosen)})
            return
        choices = None
        fewest = len(placements) + 1
        for col in required:
            compatible = active & columns[col]
            n = compatible.bit_count()
            if n == 0:
                return
            if n < fewest:
                choices, fewest = compatible, n
        while choices:
            bit = choices & -choices
            choices ^= bit
            ri = bit.bit_length() - 1
            chosen.append(ri)
            visit(active & ~conflicts(ri), required.difference(row_columns[ri]))
            chosen.pop()
            if stopped or len(solutions) >= limit:
                return

    visit((1 << len(placements)) - 1, set(range(len(columns))))
    return Search(solutions, nodes, not stopped and len(solutions) < limit)


def make_challenge(seed, missing=12, node_limit=100_000):
    """Remove clues only after exhaustive search proves the resulting puzzle unique."""
    if not 1 <= missing < len(tile_set()):
        raise ValueError("Remove between 1 and 47 tiles")
    rng = random.Random(seed)
    reference = reference_board(rng.randrange(6))
    shown = dict(reference)
    owner = {cell: i for i, cells in reference.items() for cell in cells}
    adjacent = {i: {owner[c] for c in _rim(cells) if c in owner} for i, cells in reference.items()}
    removed, rejected = set(), set()
    while len(removed) < missing:
        candidates = list(sorted(shown.keys() - rejected))
        rng.shuffle(candidates)
        # Grow connected empty regions, which give a player competing legal moves.
        candidates.sort(key=lambda i: len(adjacent[i] & removed), reverse=True)
        for i in candidates:
            cell_list = shown.pop(i)
            proof = solve(hex_board(), tile_set(), COLORS, shown, limit=2, node_limit=node_limit)
            if proof.exhausted and len(proof.solutions) == 1:
                removed.add(i)
                break
            shown[i] = cell_list
            rejected.add(i)
        else:
            raise ValueError(f"Could prove uniqueness with {len(removed)} missing tiles; try another seed or a higher node limit")
    return reference, shown, proof


PALETTE = ("#74b8c8", "#e3ad4d", "#bb809c", "#84af79")
CELL_MM = 4.5
SYMBOLS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl"
HEX_CORNERS = ((1, 1), (0, 2), (-1, 1), (-1, -1), (0, -2), (1, -1))
# An independently computed construction. Uncolored exact cover (seed 31) filled
# the board. Four-color graph coloring then balanced both the tile and cell counts.
COLORS = tuple(map(int, "130123022220003131233011112322303310231003012021"))
REFERENCE_ROWS = (
    "11PP99BB5", "PPP299BAB5", "ZZZ82MMMAA5", "ZXXZ82MfDDA5",
    "GQQXX88MfiDD3", "GGgQNX66fiii33", "GggQWNN6fciUU44",
    "GghQWWNN6fccUFE4", "hghWWlaaaacUUFjEE", "hhVVlJJJJacdFjEj",
    "VVllJLLIdddFkjj", "HVSlSSLI0dFkkk", "HHSSYLLIIRRRk",
    "HbeeYYYIROOO", "Hb7eeKYRTOC", "b77eKKTTOC", "bb7KKTTCC",
)


def reference_board(turns=0):
    """Rotate the reference tiling of the physical kit without reflecting any tile."""
    pieces = defaultdict(list)
    for r, row in enumerate(REFERENCE_ROWS, -8):
        for index, symbol in enumerate(row):
            q = max(-8, -r-8) + index
            x, y = q, r
            for _ in range(turns % 6):
                x, y = -y, x + y
            pieces[SYMBOLS.index(symbol)].append((x, y))
    return {i: tuple(sorted(cells)) for i, cells in pieces.items()}


def _outline_vertices(cells):
    # Integer corner keys give neighboring hexagons exactly coincident segments.
    edges = set()
    for q, r in cells:
        corners = [(2*q+r+x, 3*r+y) for x, y in HEX_CORNERS]
        for a, b in zip(corners, corners[1:] + corners[:1]):
            if (b, a) in edges:
                edges.remove((b, a))
            else:
                edges.add((a, b))
    next_corner = dict(edges)
    start = min(next_corner)
    loop = [start]
    while (corner := next_corner[loop[-1]]) != start:
        loop.append(corner)
    return [(x * CELL_MM * math.sqrt(3) / 2, y * CELL_MM / 2) for x, y in loop]


def _svg(width, height):
    return ET.Element("svg", xmlns="http://www.w3.org/2000/svg", width=f"{width}mm",
                      height=f"{height}mm", viewBox=f"0 0 {width} {height}")


def _draw_shape(root, cells, dx, dy, fill, edge=0.27):
    points = _outline_vertices(cells)
    path = "M " + " L ".join(f"{x+dx:.3f} {y+dy:.3f}" for x, y in points) + " Z"
    ET.SubElement(root, "path", d=path, fill=fill, stroke="#353633",
                  attrib={"stroke-width": str(edge), "stroke-linejoin": "round"})


def _label(root, x, y, text, size=3, anchor="start"):
    node = ET.SubElement(root, "text", x=f"{x:.3f}", y=f"{y:.3f}", fill="#202824",
                         attrib={"font-family": "sans-serif", "font-size": str(size), "text-anchor": anchor})
    node.text = text


def _tile(root, tile, cells, dx, dy):
    _draw_shape(root, cells, dx, dy, PALETTE[COLORS[tile]])
    # A cell center closest to the centroid keeps labels inside concave pieces.
    centers = [(CELL_MM*math.sqrt(3)*(q + r/2), CELL_MM*1.5*r) for q, r in cells]
    midx = sum(x for x, _ in centers)/len(centers)
    midy = sum(y for _, y in centers)/len(centers)
    x, y = min(centers, key=lambda p: (p[0]-midx)**2 + (p[1]-midy)**2)
    _label(root, x + dx, y + dy + 0.85, f"{tile+1:02d}", 2.55, "middle")


def _board_svg(shown, title, note):
    root = _svg(160, 145)
    _label(root, 7, 9, title, 5)
    _label(root, 7, 15, note, 2.8)
    board = hex_board()
    used = {cell for cells in shown.values() for cell in cells}
    for cell in sorted(board - used):
        _draw_shape(root, [cell], 80, 80, "#ffffff", 0.14)
    for i in sorted(shown):
        _tile(root, i, shown[i], 80, 80)
    return root


def _tiles_svg():
    root = _svg(210, 297)
    _label(root, 9, 12, "Polyhex tile set", 5)
    _label(root, 9, 19, "Print at 100% on A4. Cut on the outlines. Rotate the pieces with the numbers facing up.", 3)
    x, y, row_height = 9, 25, 0
    for i, shape in enumerate(tile_set()):
        points = _outline_vertices(shape)
        lo_x, hi_x = min(p[0] for p in points), max(p[0] for p in points)
        lo_y, hi_y = min(p[1] for p in points), max(p[1] for p in points)
        width, height = hi_x - lo_x, hi_y - lo_y
        if x + width > 201:
            x, y, row_height = 9, y + row_height + 4, 0
        _tile(root, i, shape, x-lo_x, y-lo_y)
        x += width + 4
        row_height = max(row_height, height)
    assert y + row_height <= 290, "Polyhex print sheet overflows A4"
    return root


def write_challenge(out, seed, missing=12, node_limit=100_000):
    answer, shown, proof = make_challenge(seed, missing, node_limit)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    description = {
        "seed": seed, "radius": 8, "coordinates": "axial q,r", "cell_side_mm": CELL_MM,
        "shapes": tile_set(), "colors": COLORS, "fixed": shown,
        "missing_tile_ids": sorted(answer.keys() - shown.keys()),
        "printed_id_offset": 1, "solution_count": len(proof.solutions),
        "exhaustive": proof.exhausted, "search_nodes": proof.nodes,
        "rules": "Use each tile once, without reflecting. Equal colors may not share an edge.",
    }
    (out/"challenge.json").write_text(json.dumps(description, indent=2) + "\n", encoding="utf-8")
    for name, page in (
        ("board.svg", _board_svg(shown, f"Polyhex challenge {seed}", f"Fit the {missing} missing tiles. Equal colors may not share an edge.")),
        ("solution.svg", _board_svg(answer, f"Polyhex solution {seed}", "Each tile appears once. The completed board contains 217 hexagons.")),
        ("tiles.svg", _tiles_svg()),
    ):
        ET.ElementTree(page).write(out/name, encoding="utf-8", xml_declaration=True)
    return description


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate a printable, uniquely solvable four-color polyhex game.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--missing", type=int, default=12, help="How many tiles to remove from the 48-tile board")
    parser.add_argument("--node-limit", type=int, default=100_000, help="Maximum exact-cover search nodes per uniqueness check")
    parser.add_argument("--out", type=Path, default=Path("runs/polyhex"))
    parser.add_argument("--verify", type=Path, help="Verify a saved challenge.json instead of generating")
    args = parser.parse_args(argv)
    if args.verify:
        data = json.loads(args.verify.read_text(encoding="utf-8"))
        canonical = tuple(tuple(map(tuple, shape)) for shape in data["shapes"])
        if data["radius"] != 8 or canonical != tile_set() or tuple(data["colors"]) != COLORS:
            parser.error("Challenge does not match the 217-cell board and its 48-tile colored kit")
        fixed = {int(i): tuple(map(tuple, cells)) for i, cells in data["fixed"].items()}
        result = solve(hex_board(data["radius"]), tile_set(), data["colors"], fixed, node_limit=args.node_limit)
        print(json.dumps({"solutions_found": len(result.solutions), "exhaustive": result.exhausted, "nodes": result.nodes}))
        return 0 if len(result.solutions) == 1 and result.exhausted else 1
    data = write_challenge(args.out, args.seed, args.missing, args.node_limit)
    print(f"{args.out}: {args.missing} missing tiles, one verified solution, {data['search_nodes']} search nodes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
