"""One centred, aspect-preserving photograph in the cut board's millimetre coordinates."""

import base64
import io

import numpy as np
from PIL import Image, ImageCms, ImageOps


def read_artwork(source):
    """Decode a customer image at full resolution, respecting orientation, profile and alpha."""
    with Image.open(source) as raw:
        image = ImageOps.exif_transpose(raw)
        has_alpha = "A" in image.getbands() or "transparency" in image.info
        alpha = image.convert("RGBA").getchannel("A") if has_alpha else None
        profile = image.info.get("icc_profile")
        if profile:
            if image.mode not in ("RGB", "CMYK", "L", "LAB"):
                image = image.convert("RGB")
            image = ImageCms.profileToProfile(
                image, ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                ImageCms.createProfile("sRGB"), outputMode="RGB",
            )
        else:
            image = image.convert("RGB")
        if alpha is not None:
            image.putalpha(alpha)
        return np.asarray(image)


def photo_image(pixels):
    """Accept the RGB(A) or grayscale array supplied to generate_puzzle_run."""
    values = np.asarray(pixels)
    if np.issubdtype(values.dtype, np.floating):
        if not np.isfinite(values).all():
            raise ValueError("Artwork contains non-finite pixels")
        factor = 255 if values.max() <= 1 and values.min() >= 0 else 1
        values = np.rint(np.clip(values * factor, 0, 255)).astype(np.uint8)
    image = Image.fromarray(values)
    return image if image.mode in ("RGB", "RGBA") else image.convert("RGB")


def photo_descriptor(image, width_mm, height_mm):
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return {
        "href": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii"),
        "x": 0, "y": 0, "width": round(width_mm, 4), "height": round(height_mm, 4),
        "preserveAspectRatio": "xMidYMid slice",
    }


def sample_photo(image, board_size_mm, view_mm, output_px):
    """Render a view of the SVG's xMidYMid slice mapping, e.g. a single displaced piece."""
    width, height = board_size_mm
    mm_per_pixel = max(width / image.width, height / image.height)
    left = (image.width - width / mm_per_pixel) * .5
    top = (image.height - height / mm_per_pixel) * .5
    x0, y0, x1, y1 = view_mm
    box = (left + x0 / mm_per_pixel, top + y0 / mm_per_pixel,
           left + x1 / mm_per_pixel, top + y1 / mm_per_pixel)
    patch = image.transform(output_px, Image.Transform.EXTENT, box, Image.Resampling.BICUBIC)
    if patch.mode == "RGBA":
        backing = Image.new("RGB", output_px, "white")
        backing.paste(patch, mask=patch.getchannel("A"))
        patch = backing
    return np.asarray(patch)
