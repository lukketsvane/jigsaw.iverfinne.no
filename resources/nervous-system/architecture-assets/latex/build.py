"""Build the guide with Inkscape and Tectonic (XeLaTeX and BibTeX)."""

import argparse
import csv
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parent
ASSETS = SOURCE_DIR.parent
RESEARCH = ASSETS.parent
CUT_COMPARISON = RESEARCH.parents[1] / "local" / "technical" / "cut-comparison.svg"


def executable(name, override):
    path = shutil.which(override or name)
    if not path and name == "inkscape" and not override and os.name == "nt":
        installed = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Inkscape/bin/inkscape.exe"
        if installed.is_file():
            path = str(installed)
    if not path:
        raise SystemExit(f"Could not find {override or name}. Set --{name} to the installed executable.")
    return path


def build(source, output, stage, inkscape, tectonic):
    if not source.is_file():
        raise SystemExit(f"Missing manuscript: {source}")
    plates = stage / "plates"
    plates.mkdir(parents=True, exist_ok=True)
    for item in source.parent.iterdir():
        if item.suffix.lower() in {".tex", ".bib", ".sty", ".bst", ".cls"} and item.is_file():
            shutil.copy2(item, stage / item.name)

    with CUT_COMPARISON.with_suffix(".csv").open(encoding="utf-8", newline="") as data:
        rows = csv.DictReader(data)
        with (stage / "cut-comparison-table.tex").open("w", encoding="utf-8") as table:
            print(r"\newcommand{\cutcomparisonrows}{%", file=table)
            for row in rows:
                passed = {"True": "Pass", "False": "Fail"}[row["all_hard_gates_passed"]]
                cells = [row["style"].capitalize(), row["pieces"],
                         f'{float(row["cut_length_mm"]):,.2f}',
                         f'{float(row["assembly_lock_margin_mm"]):.3f}', passed]
                print(" & ".join(cells) + r" \\", file=table)
            print("}", file=table)

    def export(svg):
        target = plates / (svg.stem + ".pdf")
        subprocess.run(
            [inkscape, str(svg), "--export-area-page", "--export-type=pdf", f"--export-filename={target}"],
            check=True,
        )

    with ThreadPoolExecutor(max_workers=3) as workers:
        list(workers.map(export, [*sorted(ASSETS.glob("*.svg")), CUT_COMPARISON]))

    for raster in [*ASSETS.glob("*.png"), *(RESEARCH / "images").glob("style_*_photo.jpg")]:
        shutil.copy2(raster, plates / raster.name)

    subprocess.run([tectonic, "--keep-logs", "--keep-intermediates", source.name], cwd=stage, check=True)
    pdf = stage / (source.stem + ".pdf")
    with pdf.open("rb") as built:
        if built.read(5) != b"%PDF-":
            raise SystemExit(f"Tectonic did not produce a PDF at {pdf}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".pdf", delete=False) as temporary:
        pending = Path(temporary.name)
    try:
        shutil.copyfile(pdf, pending)
        os.replace(pending, output)
    finally:
        pending.unlink(missing_ok=True)
    print(f"Built {output}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=SOURCE_DIR / "main.tex")
    parser.add_argument("--output", type=Path, default=RESEARCH / "architecture-guide.pdf")
    parser.add_argument("--work-dir", type=Path, help="Keep staged figures, TeX logs and bibliography output here")
    parser.add_argument("--inkscape", help="Path to the Inkscape executable")
    parser.add_argument("--tectonic", help="Path to the Tectonic executable")
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    inkscape = executable("inkscape", args.inkscape)
    tectonic = executable("tectonic", args.tectonic)
    if args.work_dir:
        stage = args.work_dir.resolve()
        if stage == source.parent:
            parser.error("--work-dir must differ from the manuscript directory")
        build(source, output, stage, inkscape, tectonic)
    else:
        with tempfile.TemporaryDirectory(prefix="jigsaw-latex-") as work:
            build(source, output, Path(work), inkscape, tectonic)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
