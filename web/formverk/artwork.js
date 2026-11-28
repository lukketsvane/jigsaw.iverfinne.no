/* Photo registration and closed silhouette geometry for the custom editor. */
(function () {
  'use strict';

  async function openImage(file) {
    if (file.size > 64 * 1024 * 1024) throw new Error('Choose an image smaller than 64 MB.');
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.src = url;
    try {
      await image.decode();
      if (!image.naturalWidth || !image.naturalHeight) throw new Error();
      return { image, url, name: file.name };
    } catch {
      URL.revokeObjectURL(url);
      throw new Error('The image could not be read.');
    }
  }

  function cropToAspect(image, aspect, frame) {
    const source = frame || { x: 0, y: 0, w: image.naturalWidth, h: image.naturalHeight };
    let { x, y, w, h } = source;
    if (w / h > aspect) {
      const width = h * aspect;
      x += (w - width) / 2;
      w = width;
    } else {
      const height = w / aspect;
      y += (h - height) / 2;
      h = height;
    }
    return { x, y, w, h };
  }

  function raster(image, crop, limit) {
    const c = crop || { x: 0, y: 0, w: image.naturalWidth, h: image.naturalHeight };
    const scale = Math.min(1, limit / Math.max(c.w, c.h));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(c.w * scale));
    canvas.height = Math.max(1, Math.round(c.h * scale));
    canvas.getContext('2d').drawImage(image, c.x, c.y, c.w, c.h, 0, 0, canvas.width, canvas.height);
    return canvas;
  }

  function printImage(image, crop) {
    const canvas = raster(image, crop, 4096);
    const ctx = canvas.getContext('2d');
    ctx.globalCompositeOperation = 'destination-over';
    ctx.fillStyle = 'white';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    return canvas.toDataURL('image/jpeg', 0.94);
  }

  async function trace(image, maskFile) {
    const canvas = raster(image, null, 1024);
    if (maskFile) {
      const ctx = canvas.getContext('2d');
      const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const rgba = pixels.data;
      let transparent = 0;
      for (let i = 3; i < rgba.length; i += 4) if (rgba[i] < 64) transparent++;
      if (!transparent) {
        const luma = (i) => 0.299 * rgba[i] + 0.587 * rgba[i + 1] + 0.114 * rgba[i + 2];
        const border = [];
        for (let x = 0; x < canvas.width; x++) {
          border.push(luma(x * 4), luma(((canvas.height - 1) * canvas.width + x) * 4));
        }
        for (let y = 0; y < canvas.height; y++) {
          border.push(luma(y * canvas.width * 4), luma(((y + 1) * canvas.width - 1) * 4));
        }
        border.sort((a, b) => a - b);
        const background = border[Math.floor(border.length / 2)];
        let foreground = 0;
        for (let i = 0; i < rgba.length; i += 4) {
          rgba[i + 3] = Math.abs(luma(i) - background) > 50 ? 255 : 0;
          if (rgba[i + 3]) foreground++;
        }
        if (!foreground)
          throw new Error('The mask needs a visible shape against a contrasting or transparent background.');
        ctx.putImageData(pixels, 0, 0);
      }
    }
    const response = await fetch('/api/outline', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artwork_data_url: canvas.toDataURL('image/png') }),
    });
    const result = await response.json();
    if (!response.ok || result.error) throw new Error(result.error || 'Outline extraction failed.');
    return result.points;
  }

  function bounds(points) {
    if (
      !Array.isArray(points) ||
      points.length < 3 ||
      !points.every((p) => Array.isArray(p) && p.length === 2 && p.every(Number.isFinite))
    ) {
      throw new Error('A closed shape needs at least three distinct points.');
    }
    const xs = points.map((p) => p[0]),
      ys = points.map((p) => p[1]);
    const x = Math.min(...xs),
      y = Math.min(...ys);
    const w = Math.max(...xs) - x,
      h = Math.max(...ys) - y;
    if (!(w > 0 && h > 0)) throw new Error('The outline must enclose an area.');
    return { x, y, w, h };
  }

  function makeShape(points, frame, keepPhotoCrop = false) {
    const b = bounds(points);
    const norm = points.map(([x, y]) => [(x - b.x) / b.w, (y - b.y) / b.h]);
    const crop = {
      x: frame.x + b.x * frame.w,
      y: frame.y + b.y * frame.h,
      w: b.w * frame.w,
      h: b.h * frame.h,
    };
    const cross = (p, q) => p[0] * q[1] - q[0] * p[1];
    const area = Math.abs(norm.reduce((a, p, i) => a + cross(p, norm[(i + 1) % norm.length]), 0));
    if (area < 0.001) throw new Error('The outline is too narrow or crosses itself.');
    return { points: norm, aspect: crop.w / crop.h, crop: keepPhotoCrop ? crop : null };
  }

  function drawable(points) {
    bounds(points);
    function turn(a, b, c) {
      return Math.sign((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]));
    }
    function between(a, b, p) {
      return (
        p[0] >= Math.min(a[0], b[0]) &&
        p[0] <= Math.max(a[0], b[0]) &&
        p[1] >= Math.min(a[1], b[1]) &&
        p[1] <= Math.max(a[1], b[1])
      );
    }
    const n = points.length;
    for (let i = 0; i < n; i++) {
      const a = points[i],
        b = points[(i + 1) % n];
      for (let j = i + 2; j < n; j++) {
        if (i === 0 && j === n - 1) continue;
        const c = points[j],
          d = points[(j + 1) % n];
        const abC = turn(a, b, c),
          abD = turn(a, b, d),
          cdA = turn(c, d, a),
          cdB = turn(c, d, b);
        if (
          (abC * abD < 0 && cdA * cdB < 0) ||
          (!abC && between(a, b, c)) ||
          (!abD && between(a, b, d)) ||
          (!cdA && between(c, d, a)) ||
          (!cdB && between(c, d, b))
        ) {
          throw new Error('The outline crosses itself. Undo a point or redraw it.');
        }
      }
    }
  }

  function asSvg(shape) {
    const pts = shape.points
      .map(([x, y]) => [x * 1000, (y * 1000) / shape.aspect].map((n) => n.toFixed(4)).join(','))
      .join(' ');
    return '<svg xmlns="http://www.w3.org/2000/svg"><polygon points="' + pts + '"/></svg>';
  }

  window.FormverkArtwork = { openImage, cropToAspect, printImage, trace, makeShape, drawable, asSvg };
})();
