# SOUND IN PIECES / site

`public/` is the site at jigsaw.iverfinne.no (Vercel root `web/sound-in-pieces`, output `public/`). `src/` rebuilds it.

```
cd web/sound-in-pieces/src
npm install
python build.py            # template.html, design.html, designer_*.js + web/fossilnet -> public/, ../vercel.json
npm run hero               # hero3d.js -> public/js/hero.js
python pathnet_export.py   # pathnet/decoder.json (APHEX LOGO) -> public/js/pathnet-logo.js
```

The renders, `scrollzip/05-cut-field.svg` and `puzzle.glb` come from the Figma files and the Drive working folder. They stay private in `runs/sound-in-pieces/` (or `SIP_SRC`); rebuild from them only when they change:

```
python images.py   # renders -> public/img, images.json; film grain tile
python pieces.py   # cut field -> public/data/pieces.json
python icons.py    # piece 093 -> public/icons, public/splash, splash-links.html
npm run model      # puzzle.glb -> public/models/puzzle.js, its print -> public/img/print-1600.webp
```

The live cut below the homepage hero uses the designer's PathNet model to generate silhouettes, pack them and trace shared seams. Tap for a new layout or drag sideways to reshape the cut.

`build.py` reads the upper melody track from `public/media/melody.mid` and embeds its pitches and velocities in the page. Taps on the live cut, 3D board, featured piece, cut field, and atlas advance the same melody one chord at a time. It loops after the last chord.
