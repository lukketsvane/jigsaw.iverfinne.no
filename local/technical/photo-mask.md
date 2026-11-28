# From a photograph to a foreground mask

Nervous System credited Daniel Gatis's **rembg** for Magic Shape, which suggests an outline for a photo puzzle. The archive contains rembg 1.0.16 from November 13, 2020, four days before the studio announcement. It can reproduce a saliency mask locally. The studio's model revision, checkpoint and rules for converting a mask into a cuttable perimeter remain undocumented. [Studio announcement](https://n-e-r-v-o-u-s.com/blog/?p=8656) · [Pinned source and provenance](../../resources/nervous-system/upstream/rembg/SOURCE.md)

## What U²-Net learns

A residual U-block first projects the incoming features with a 3 × 3 convolution, batch normalization and ReLU. An inner encoder and decoder gather context at multiple scales. Skip connections restore finer detail as the decoder upsamples. The block adds this result to its projected input. At the deepest outer stages, the flat block `RSU4F` uses convolutions with dilation rates 1, 2, 4 and 8 to enlarge its field of view at fixed resolution. [Original paper, §§3.1–3.2 and Table 1](../../resources/nervous-system/papers/U2-Net-2005.09007v2.pdf)

| Outer network component | Implementation in the pinned source |
| --- | --- |
| Encoder | Six stages use RSU7, RSU6, RSU5, RSU4, RSU4F and RSU4F. Five 2 × 2 max pools reduce the 320-pixel input width to 160, 80, 40, 20 and 10. The full encoder has 64, 128, 256, 512, 512 and 512 output channels. |
| Decoder | Five stages bilinearly upsample features and concatenate them with the matching encoder features. The compact model retains the same connections, with 16 internal and 64 output channels per block. |
| Prediction | A 3 × 3 convolution at the bottleneck and at each of the five decoder stages produces six single-channel maps. After alignment to 320 × 320, a learned 1 × 1 convolution fuses their logits. Sigmoids yield the fused map and six side maps. |

The authors trained from scratch on 10,553 DUTS-TR images with foreground labels. Offline horizontal flips doubled the training set. During training, images were resized to 320 × 320, randomly flipped vertically and cropped to 288 × 288. The objective sums seven equally weighted pixelwise binary cross-entropy losses, one per side map and one for the fused map. The authors report Xavier initialization and 600,000 Adam updates with batch size 12 and initial learning rate 0.001. The replay loads the learned weights and runs a forward pass. [Original paper, §§3.3–4.3](../../resources/nervous-system/papers/U2-Net-2005.09007v2.pdf) · [Network implementation](https://github.com/danielgatis/rembg/blob/332c16b58e0194f0960f9d6cf38746e7be8546b7/src/rembg/u2net/u2net.py)

## What the replay computes

1. Decode the photograph as RGB and resize it to a 320 × 320 square. Divide by the maximum channel value over the resized image. Subtract RGB means `(0.485, 0.456, 0.406)` and divide by standard deviations `(0.229, 0.224, 0.225)`. Convert to a float32 tensor shaped `[1, 3, 320, 320]`.
2. Run the model in evaluation mode with gradients disabled. Take the first returned tensor, which is the fused sigmoid map. Stretch that image's scores to the unit interval using `(score − minimum) / (maximum − minimum)`. The implementation assumes a nonzero input maximum and a nonconstant prediction.
3. Multiply by 255 and convert to an 8-bit image. The wrapper converts to grayscale and restores the original width and height using Lanczos resampling. The PNG stores a relative saliency score at each pixel. Thresholding and contour extraction are separate operations. [Preprocessing](https://github.com/danielgatis/rembg/blob/332c16b58e0194f0960f9d6cf38746e7be8546b7/src/rembg/u2net/data_loader.py) · [Prediction](https://github.com/danielgatis/rembg/blob/332c16b58e0194f0960f9d6cf38746e7be8546b7/src/rembg/u2net/detect.py) · [Local replay](../../resources/nervous-system/upstream/rembg/replay.py)

## Reproduce the CPU measurement

Use PowerShell from the repository root and a Python 3.11 environment. Install the dependencies once, then work from the archived rembg directory. These versions match the CPU replay.

```powershell
python -m pip install torch==2.14.1 torchvision==0.29.1 numpy==1.26.4 Pillow==12.1.0 scikit-image==0.26.0 requests==2.32.4 matplotlib==3.11.2 tqdm==4.70.1
Set-Location resources/nervous-system/upstream/rembg
```

The two weight files are stored locally outside Git. For a fresh checkout, retrieve the missing checkpoints from the authors' file IDs recorded by rembg.

```powershell
New-Item -ItemType Directory -Force weights | Out-Null
if (!(Test-Path weights/u2net.pth)) {
  curl.exe --fail --location 'https://drive.usercontent.google.com/download?id=1ao1ovG1Qtx4b7EoskHXmi2E9rp5CHLcZ&export=download&confirm=t' --output weights/u2net.pth
}
if (!(Test-Path weights/u2netp.pth)) {
  curl.exe --fail --location 'https://drive.usercontent.google.com/download?id=1rbSTGKAE-MTxBYHd-51l2hMOQPT_7EPy&export=download&confirm=t' --output weights/u2netp.pth
}
```

The replay verifies SHA-256 for the ZIP and the selected checkpoint before importing the archived model and loading the tensor weights. With the environment and these files in place, inference runs offline.

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `rembg-332c16b58e01.zip` | 15,504,263 | `86de06c970475cbb63d56b458f66f3548b1ccd08639f1aa439bb0b2cb333bfb6` |
| `weights/u2net.pth` | 176,290,937 | `10025a17f49cd3208afc342b589890e402ee63123d6f2d289a4a0903695cce58` |
| `weights/u2netp.pth` | 4,683,258 | `e7567cde013fb64813973ce6e1ecc25a80c05c3ca7adbc5a54f3c3d90991b854` |

```powershell
$env:CUDA_VISIBLE_DEVICES = '-1'
$env:OMP_NUM_THREADS = '2'
$env:MKL_NUM_THREADS = '2'
python replay.py --model u2net  --output (Join-Path $env:TEMP 'u2net-mask.png')
python replay.py --model u2netp --output (Join-Path $env:TEMP 'u2netp-mask.png')
```

Both commands read `examples/animal-1.jpg` from the ZIP, an RGB photograph of a white tiger measuring 1000 × 667 pixels. For another photo, add `--image "C:\path\to\photo.jpg"` to either command. The script prints the mask dimensions and range for every input. Omitting `--output` suppresses the PNG.

| Model | Output size and range | Intersection pixels | Union pixels | Foreground IoU |
| --- | --- | ---: | ---: | ---: |
| Full U²-Net | 1000 × 667, 0–255 | 163,638 | 163,776 | 0.99916 |
| Compact U²-Net | 1000 × 667, 0–255 | 160,115 | 164,705 | 0.97213 |

For this fixture, the script labels a pixel foreground when its value is at least 128 in the predicted mask or, independently, in the alpha channel of the published example `animal-1.out.png`. Intersection over union (IoU) divides the count marked in both by the count marked in either. These measurements quantify agreement with one upstream example. The full CPU replay differed from the archived grayscale output by one intensity level at eight pixels, with identical foreground membership at 128. The figure illustrates the largest exterior threshold contour as a research diagnostic. [Archived full-model mask](../../resources/nervous-system/upstream/rembg/animal-1-mask.png) · [Photograph, mask and threshold contour](../../resources/nervous-system/architecture-assets/mask-flow.png)
