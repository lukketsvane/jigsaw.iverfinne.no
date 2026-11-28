# Background removal for Magic Shape

In November 2020 Nervous System identified Daniel Gatis's rembg as the library behind the custom puzzle maker's suggested photo outline. The library predicts a foreground mask. The studio did not publish its exact revision, choice of checkpoint, or the steps it took to turn that mask into a manufacturable perimeter. [Studio announcement](https://n-e-r-v-o-u-s.com/blog/?p=8656)

The [complete upstream source ZIP](rembg-332c16b58e01.zip) contains rembg 1.0.16, including its MIT license, inference code, model definitions, command-line tool, dependency list and example image pairs. Commit `332c16b58e0194f0960f9d6cf38746e7be8546b7` is dated November 13, four days before the announcement. All 39 files were compared to the Git blobs at that revision. The ZIP is 15,504,263 bytes, SHA-256 `86de06c970475cbb63d56b458f66f3548b1ccd08639f1aa439bb0b2cb333bfb6`. [Pinned source](https://github.com/danielgatis/rembg/tree/332c16b58e0194f0960f9d6cf38746e7be8546b7) · [Original ZIP](https://codeload.github.com/danielgatis/rembg/zip/332c16b58e0194f0960f9d6cf38746e7be8546b7)

## Pretrained models

Both checkpoints below came from the Google Drive file IDs in this revision's loader. The repository ignores `.pth` files, so a fresh Git checkout needs the model downloaded into a `weights` directory beside this document. The replay verifies its SHA-256 before loading it. [Original loader](https://github.com/danielgatis/rembg/blob/332c16b58e0194f0960f9d6cf38746e7be8546b7/src/rembg/u2net/detect.py) · [Model authors](https://github.com/xuebinqin/U-2-Net) · [Local paper](../../papers/U2-Net-2005.09007v2.pdf)

| Download | Local filename under `weights/` | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| [U²-Net, upstream default](https://drive.usercontent.google.com/download?id=1ao1ovG1Qtx4b7EoskHXmi2E9rp5CHLcZ&export=download&confirm=t) | `u2net.pth` | 176290937 | `10025a17f49cd3208afc342b589890e402ee63123d6f2d289a4a0903695cce58` |
| [U²-Net compact](https://drive.usercontent.google.com/download?id=1rbSTGKAE-MTxBYHd-51l2hMOQPT_7EPy&export=download&confirm=t) | `u2netp.pth` | 4683258 | `e7567cde013fb64813973ce6e1ecc25a80c05c3ca7adbc5a54f3c3d90991b854` |

## Reproduce the foreground mask

The [local replay script](replay.py) imports the original network, preprocessing and prediction code directly from the ZIP. It loads only tensor weights and uses the upstream resize filter to bring the mask back to the input dimensions. With no input specified it compares against the alpha channel in the upstream example `animal-1.out.png`. Both masks are thresholded at 128. The score divides their shared foreground area by the total area marked as foreground by either mask.

Run these commands from this directory in a Python 3.11 virtual environment. The first two replay commands use the bundled example. The last accepts your own photograph.

```sh
python -m pip install torch torchvision numpy Pillow scikit-image requests matplotlib tqdm
python replay.py --model u2net
python replay.py --model u2netp
python replay.py --image my-photo.jpg --output my-mask.png
```

| Model | Input dimensions | Mask range | Foreground intersection over union |
| --- | --- | --- | ---: |
| U²-Net | 1000 × 667 | 0–255 | 0.99916 |
| U²-Net compact | 1000 × 667 | 0–255 | 0.97213 |

Both runs used CPU inference, PyTorch 2.14.1, torchvision 0.29.1, NumPy 1.26.4, Pillow 12.1.0 and scikit-image 0.26.0. The [saved mask](animal-1-mask.png) is from the full model. The reference is an example published by rembg, so the comparison measures reproduction of that output. The studio's production outlines are a separate benchmark.
