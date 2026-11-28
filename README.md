# Jigsaw

Whole-piece neural SVG geometry and constrained puzzle composition for laser cutting.

## Øglepuslespelet

Lag eit kantlaust puslespel med 216 øgler. Kvar brikke har fire føter, krumma hale og sin eigen farge. Øgler på venstresida passar til naboane på høgresida, og det same gjeld oppe og nede. Forma og fargefeltet endrar seg med frøet. Utforminga byggjer på [Nervous System sitt Lizard Infinity Puzzle](https://n-e-r-v-o-u-s.com/shop/product.php?code=387).

Køyr frå prosjektrota med pakkane i `requirements.txt`.

```powershell
.\.venv\Scripts\python.exe -m pathnet.lizards --seed 387 --color-mode solid --output runs/lizards-387
Start-Process runs/lizards-387/index.html
```

`index.html` er eit sjølvstendig spel som verkar utan nett. Du kan starte med åtte lause brikker eller blande alle. `artwork.svg` og `cut.svg` har same mål og koordinatsystem. Kvar fysisk saum står éin gong i kuttefila. Éin repetisjon er 500 mm brei. Med heile føter og halar er kuttefila for frø 387 om lag 528 × 421 mm. `assembly.png` viser heile puslespelet, og `detail.png` viser korleis dei lause øglene passar inn.

Tre kurver og 120-gradars rotasjonar definerer grunnøgla. Eit trena konvolusjonsnett lærer korleis eit elastisk ark bøyer seg rundt lokale trekkpunkt. Øgler, auge og ryggstrekar blir flytta gjennom det same periodiske feltet. Avgrensa lokale hellingar hindrar at feltet brettar seg. Kvar eksport kontrollerer dei avrunda saumane, dekninga over begge kantane og at kuttefila dannar nøyaktig dei same lukka brikkene som spelet. Fysisk passform avheng òg av materiale og snittbreidd.

Dei trente vektene ligg i `pathnet/lizard_elastic.npz`. Desse kommandoane viser treningsmåla, trenar ein ny modell til ei eiga fil og testar 64 brett gjennom SVG-eksport og ny innlesing.

```powershell
.\.venv\Scripts\python.exe -m pathnet.lizard_net report
.\.venv\Scripts\python.exe -m pathnet.lizard_net train --output runs/lizards-387/retrained.npz
.\.venv\Scripts\python.exe -m pathnet.lizards --seed 356 --check-seeds 64 --output runs/lizards-387
```

## Nordic rock-art animal puzzles

The `heller_jigsaw` generator fits shared seams to silhouettes from the local Nordic rock-art catalog. Use `--family animal` to fill every slot from the animal records. The run records the target asset, site, creator and license for each piece.

Run these commands from the repository root on Windows with the existing virtual environment and processed catalog.

```powershell
.\.venv\Scripts\python.exe -m heller_jigsaw generate --family animal --pieces 6 --classes "reindeer,elk_moose,bear,whale_cetacean,bird,fish" --width-mm 220 --outline organic --seed 42 --out runs/nordic-animals-6
.\.venv\Scripts\python.exe -m heller_jigsaw validate --run-dir runs/nordic-animals-6
.\.venv\Scripts\python.exe -m heller_jigsaw train-encoder --seed 42 --epochs 12 --img-size 96 --out runs/nordic-encoder-42
```

Open `pieces_contact_sheet.png` to compare the carved motifs with the generated pieces, or `pieces_unlabeled_sheet.png` to judge recognition from the outlines alone. `cut.svg` contains the shared cut lines in millimetres. `quality.json` records the geometry and SVG checks. `metrics.csv` records silhouette overlap per piece. The recognizer checkpoint and its held-out evaluation are written to the encoder output folder.

The [corpus pipeline](hellerisninger/pipeline/README.md) builds the catalog and masks. Add `--synthetic` to exercise generation with built-in test shapes.

## APHEX PathNet

[`pathnet/`](pathnet/README.md) predicts closed cubic Bézier shapes, with a dedicated Aphex Twin logo class, then arranges them into a shared cutting network.

```sh
cd pathnet
python -m pip install -r requirements.txt
python generate.py --seed 22 --count 60 --output ./generated
python puzzle.py --seed 22 --width 300 --height 220 --motifs 24 --logo-share 0.85 --output ./generated
python field.py --seed 22 --pieces 32 --output ./generated/my-board
python train.py
```

`puzzle.py` places logos and fills the leftover space with filler pieces. `field.py` makes every piece a logo and divides the whole board between them; its [README section](pathnet/README.md#field-composition-every-piece-a-logo) covers how the gaps are shared, how a saved board is resumed, and what the interlock measures mean.

The supplied typeface is not included. The source logo, glyph-derived dataset, and generated geometry have not been cleared for redistribution or commercial use. Physical laser testing and mechanical interlocking remain unverified.


## Free-form production experiments (V10)

Install the dependencies in `requirements.txt`. On a new GPU machine see [Shadow PC migration](SHADOW_PC.md).

The source-only checkout keeps large model and raster files in private storage. An ordered catalog of 2?32 motifs drives the free-form solver:

```powershell
python -m heller_jigsaw compose --catalog runs/scale32_targets/aphex/catalog.json --seed 103 --mode 3 --out runs/layout
python -m heller_jigsaw lock --run-dir runs/layout --out runs/locked --method standard
python -m heller_jigsaw audit-fabrication --run-dir runs/locked --rigid
```

Use `--method mincost` for the alternative minimum-cost spanning seam construction. The four layout modes vary the placement objective; each explores six outline configurations. A reference catalog can rank modified targets against their original silhouettes. Outputs are separate, so validated runs cannot be overwritten by these commands.

The fabrication screen checks re-imported SVG geometry, four kerf widths (0.10?0.25 mm), conservative 3 mm neck erosion, sampled translational motion and thin-feature loss. The optional rigid screen samples individual planar motions; it does not prove group rotation is impossible. Reports are not a physical certification.

V10 evaluated 72 layouts, 432 partition configurations and 46 locking finalists across Animal, Aphex and Helleristning editions. The 96px CC0-only model was selected over the 128px model by validation performance. The original source silhouettes, their licenses and the previous successful runs remain separate from modified manufacturing targets. The V11 robust-target investigation is experimental.

The private transfer tool is described in [SHADOW_PC.md](SHADOW_PC.md).
