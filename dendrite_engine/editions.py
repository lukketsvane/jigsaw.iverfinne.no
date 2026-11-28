"""Local Studio entry points for specialty puzzles and their generated files."""

import json
from pathlib import Path
import tempfile
import uuid

from .cli import main


FAMILIES = {
    "torus": ("Torus", "Opposite edges meet by translation."),
    "klein": ("Klein bottle", "Flip pieces across one pair of edges."),
    "projective": ("Projective plane", "Both pairs of edges meet after a flip."),
    "sphere": ("Spherical map", "An unfolded icosahedral map with paired seams."),
    "spectre": ("Spectre", "A single tile shape covers an aperiodic patch."),
    "polyhex": (
        "Polyhex challenge",
        "Fill the missing cells without touching equal colors.",
    ),
    "dual": ("Two assemblies", "Rearrange pieces from a rectangle into a disc."),
    "nested": ("Nested bubbles", "Connect miniature puzzles with keyed seams."),
    "eclipse": ("Eclipse", "Turn over groups of pieces to change the visible disc."),
    "chess": ("Chess", "Build the board and fit 32 playing pieces into its border."),
}


def edition_files(folder):
    files = [
        p.name
        for p in sorted(folder.iterdir())
        if p.suffix in (".svg", ".png", ".json") and p.is_file()
    ]
    preview = next(
        (
            s
            for s in (
                "six-states.png",
                "four-states.png",
                "playing.svg",
                "storage.svg",
                "disc-assembly.svg",
                "board.svg",
                "artwork.svg",
                "preview.png",
            )
            if s in files
        ),
        None,
    )
    return {"folder": folder.name, "files": files, "preview": preview}


def catalog(root):
    return [
        {
            "id": k,
            "name": name,
            "description": description,
            "sample": edition_files(root / k) if (root / k).is_dir() else None,
        }
        for k, (name, description) in FAMILIES.items()
    ]


def generate_edition(root, family, seed):
    if family not in FAMILIES or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise ValueError("Select an edition and enter a seed from 0 to 4294967295.")
    root.mkdir(parents=True, exist_ok=True)
    run_id = f"{family}-{uuid.uuid4().hex}"
    args = (
        ["infinity", "--topology", family]
        if family in ("torus", "klein", "projective", "sphere")
        else [family]
    )
    with tempfile.TemporaryDirectory(dir=root, prefix=".building-") as tmp:
        stage = Path(tmp) / run_id
        code = main([*args, "--seed", str(seed), "--out", str(stage)])
        if code:
            raise ValueError("This edition could not validate the selected seed.")
        final = root / run_id
        # Some solvers record absolute paths to the original cut in their reports.
        for path in stage.glob("*.json"):
            body = path.read_text(encoding="utf-8")
            old = json.dumps(str(stage))[1:-1]
            if old in body:
                path.write_text(
                    body.replace(old, json.dumps(str(final))[1:-1]), encoding="utf-8"
                )
        stage.rename(final)
    return edition_files(final)
