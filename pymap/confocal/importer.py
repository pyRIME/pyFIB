"""Confocal importer: turn any supported file into a ConfocalImage.

Backends, tried by extension, each degrading gracefully:
  .lif            -> readlif   (raises a clear message if readlif missing)
  .tif/.tiff/.ome -> tifffile  (OME/ImageJ axes, channels, colours, pixel size);
                     if tifffile can't decode the pixels (LZW/deflate without
                     imagecodecs) Pillow decodes the pages into the same shape;
                     a plain multipage TIFF reads <=6 gray pages as channels,
                     more as z; without tifffile, PIL multipage (pages as z)
  everything else -> PIL       (RGB -> premerged; gray/16-bit -> one channel)

Z is projected at import (MIP default; mean/slice available). The registration
core never sees the Z or channel dimensions.
"""

from __future__ import annotations

from typing import Optional, Callable, List, Tuple
import numpy as np
from PIL import Image

from .model import (Channel, ConfocalImage, DEFAULT_PALETTE, autoscale,
                    default_window)
import logging

log = logging.getLogger("pymap.confocal")

PROJECTIONS = ("mip", "mean", "slice")


class ConfocalImportError(Exception):
    pass


def _denom(dtype):
    import numpy as _np
    if dtype == _np.uint8:
        return 255.0
    if dtype == _np.uint16:
        return 65535.0
    return None   # float / other -> normalize by data max later


def _finite(a, what="image"):
    """NaN -> 0, +inf -> the largest finite value, -inf -> 0 (v0.9.2: a float TIFF
    with NaN, e.g. from Fiji or deconvolution, broke Open Confocal)."""
    a = np.asarray(a, dtype=np.float32)
    bad = ~np.isfinite(a)
    if not bad.any():
        return a
    n_bad = int(bad.sum())
    fin = a[~bad]
    top = float(fin.max()) if fin.size else 0.0
    log.warning("%s: %d NaN/inf pixels (of %d) set to 0 (inf to the largest finite "
                "value %g)", what, n_bad, a.size, top)
    out = np.nan_to_num(a, nan=0.0, posinf=top, neginf=0.0)
    return out


def _to_true(plane, denom):
    """Normalize a projected plane to [0,1] true intensity (by dtype, not min-max)."""
    plane = _finite(plane, "channel")
    if denom is None:
        m = float(plane.max()) if plane.size else 0.0
        denom = m if m > 0 else 1.0
    return np.clip(plane / denom, 0.0, 1.0)


def _make_channel(plane, denom, name, color):
    data = _to_true(plane, denom)
    lo, hi = default_window(data)
    return Channel(data=data, name=name, color=color, bit_depth=16,
                   low=lo, high=hi)


def _imagej_colors(ij_meta):
    """Per-channel LUT colours from ImageJ metadata (the file's own colours)."""
    luts = ij_meta.get("LUTs") if ij_meta else None
    if not luts:
        return []
    out = []
    for lut in luts:
        lut = np.asarray(lut)
        if lut.shape[0] >= 3:
            r, g, b = float(lut[0][-1]), float(lut[1][-1]), float(lut[2][-1])
            m = max(r, g, b, 1.0)
            out.append((r / m, g / m, b / m) if (r + g + b) > 0 else None)
        else:
            out.append(None)
    return out


# --------------------------------------------------------------------------- #
# projection helpers
# --------------------------------------------------------------------------- #
def _proj_stack(stack: np.ndarray, projection: str, z_index: Optional[int]) -> np.ndarray:
    """stack is (Z, Y, X) -> (Y, X)."""
    if stack.ndim == 2:
        return stack.astype(np.float32)
    if projection == "mean":
        return stack.mean(axis=0).astype(np.float32)
    if projection == "slice":
        i = 0 if z_index is None else int(np.clip(z_index, 0, stack.shape[0] - 1))
        return stack[i].astype(np.float32)
    return stack.max(axis=0).astype(np.float32)   # mip (default)


def _palette(i: int):
    return DEFAULT_PALETTE[i % len(DEFAULT_PALETTE)]


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
def import_confocal(path: str, projection: str = "mip",
                    z_index: Optional[int] = None,
                    series: Optional[int] = None) -> ConfocalImage:
    """series: which image of a multi-image Leica .lif (None = the first)."""
    low = path.lower()
    if low.endswith(".lif"):
        return _from_lif(path, projection, z_index, series)
    if low.endswith((".tif", ".tiff")):
        return _from_tiff(path, projection, z_index)
    return _from_pil(path)


# --------------------------------------------------------------------------- #
# PIL backend (png/jpg/bmp, and multipage TIFF fallback)
# --------------------------------------------------------------------------- #
def _from_pil(path: str) -> ConfocalImage:
    img = Image.open(path)
    frames = getattr(img, "n_frames", 1)
    log.info("Pillow: %s mode %s size %s, %d frame(s)", img.format, img.mode, img.size, frames)

    if frames > 1:
        stack = []
        for i in range(frames):
            img.seek(i)
            conv = img.convert("I") if img.mode.startswith("I") else img.convert("L")
            stack.append(np.asarray(conv))
        st = np.stack(stack, axis=0)
        plane = _proj_stack(st.astype(np.float32), "mip", None)
        denom = _denom(st.dtype)
        return ConfocalImage(channels=[_make_channel(plane, denom, "Ch1", _palette(0))],
                             source=path, n_z=frames, projection="mip")

    mode = img.mode
    if mode in ("RGB", "RGBA", "P"):
        rgb = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
        return ConfocalImage(premerged_rgb=rgb, source=path, n_z=1)
    if mode.startswith("I") or mode == "F":
        arr = np.asarray(img)
        return ConfocalImage(channels=[_make_channel(arr, _denom(arr.dtype),
                                                     "Ch1", _palette(0))],
                             source=path, n_z=1)
    arr = np.asarray(img.convert("L"))
    return ConfocalImage(channels=[_make_channel(arr, 255.0, "Ch1", _palette(0))],
                         source=path, n_z=1)


# --------------------------------------------------------------------------- #
# tifffile backend (OME-TIFF, ImageJ hyperstacks)
# --------------------------------------------------------------------------- #
IMAGECODECS_HINT = ("install the 'imagecodecs' package (Python 3.9: "
                    "pip install imagecodecs==2024.6.1; newer Python: pip install imagecodecs)")

# A plain multipage TIFF (no OME/ImageJ metadata) has one generic page axis.
# Up to this many pages are read as channels (each gets its own display controls);
# more are read as a z stack and projected.
MAX_PAGES_AS_CHANNELS = 6
_GENERIC_AXES = "IQ"     # tifffile's generic page axis ('I'; 'Q' in older releases)


def _from_tiff(path: str, projection: str, z_index: Optional[int]) -> ConfocalImage:
    try:
        import tifffile
    except ImportError:
        return _from_pil(path)   # graceful: pages-as-z

    notes: List[str] = []
    decode_error = None
    arr = None
    try:
        with tifffile.TiffFile(path) as tf:
            series = tf.series[0]
            axes = series.axes.upper()
            shape = tuple(int(n) for n in series.shape)
            ome = tf.ome_metadata
            ij = tf.imagej_metadata
            log.info("tifffile: series 0 of %d, axes %s, shape %s, dtype %s, %d pages, "
                     "compression %s, ome=%s imagej=%s", len(tf.series), axes, shape,
                     series.dtype, len(tf.pages),
                     getattr(tf.pages[0], "compression", "?") if len(tf.pages) else "-",
                     bool(ome), bool(ij))
            try:
                arr = np.asarray(series.asarray())
            except Exception as e:  # noqa: BLE001  (e.g. LZW/deflate without imagecodecs)
                decode_error = e
                log.warning("tifffile could not decode (%s: %s); trying Pillow",
                            type(e).__name__, e)
    except Exception as e:  # noqa: BLE001  (not a TIFF tifffile understands)
        log.warning("tifffile could not open %s (%s: %s); trying Pillow", path,
                    type(e).__name__, e)
        try:
            return _from_pil(path)
        except Exception:  # noqa: BLE001
            raise ConfocalImportError("Could not read %s: %s" % (path, e)) from e

    if arr is None:
        arr = _pil_pages(path, shape, axes, decode_error)
        notes.append("decoded with Pillow")

    axes, note = _interpret_generic_axis(arr, axes)
    if note:
        notes.append(note)

    denom = _denom(arr.dtype)
    chans, premerged = _split_axes(arr, axes, projection, z_index)

    if premerged is not None:
        pix = _ome_pixel_size(ome)
        return ConfocalImage(premerged_rgb=premerged, source=path,
                             n_z=_z_len(arr, axes), projection=projection,
                             pixel_size_m=pix, notes=notes)

    names, colors, pix = _parse_ome(ome)
    if not colors:
        colors = _imagej_colors(ij)
    channels = []
    for i, plane in enumerate(chans):
        name = names[i] if i < len(names) and names[i] else "Ch%d" % (i + 1)
        col = colors[i] if i < len(colors) and colors[i] else _palette(i)
        channels.append(_make_channel(plane, denom, name, col))
    return ConfocalImage(channels=channels, source=path, pixel_size_m=pix,
                         n_z=_z_len(arr, axes), projection=projection, notes=notes)


def _pil_pages(path: str, shape, axes, decode_error) -> np.ndarray:
    """Decode the series' pages with Pillow (which reads LZW/PackBits natively) and
    give them tifffile's series shape, so channels and z survive. Used when tifffile
    can't decode, usually LZW or deflate without the 'imagecodecs' package."""
    want = int(np.prod(shape)) if shape else 0
    try:
        img = Image.open(path)
        n = getattr(img, "n_frames", 1)
        pages = []
        got = 0
        for i in range(n):
            img.seek(i)
            page = np.asarray(img)
            pages.append(page)
            got += page.size
            if want and got >= want:
                break
        stack = np.stack(pages, axis=0)
        if (stack.ndim == 4 and "S" in axes and "Y" in axes
                and axes.index("S") < axes.index("Y")):
            stack = np.moveaxis(stack, -1, 1)        # planar RGB: series is ...SYX
        if want and stack.size != want:
            raise ValueError("Pillow read %d values, expected %d for shape %s"
                             % (stack.size, want, shape))
        return stack.reshape(shape) if want else stack
    except Exception as pil_error:  # noqa: BLE001
        raise ConfocalImportError(
            "Could not decode %s.\n\ntifffile: %s\nPillow: %s\n\nIf the file is "
            "LZW- or deflate-compressed, %s."
            % (path, decode_error, pil_error, IMAGECODECS_HINT)) from pil_error


def _interpret_generic_axis(arr, axes):
    """A lone generic page axis ('I'/'Q') with no C or Z axis comes from a plain
    multipage TIFF. Read few gray pages as channels, otherwise as z, rather than
    dropping all but the first page. Returns (axes, note-or-None)."""
    gen = [a for a in axes if a in _GENERIC_AXES]
    if len(gen) != 1 or "C" in axes or "Z" in axes:
        return axes, None
    a = gen[0]
    n = arr.shape[axes.index(a)]
    if n <= 1:
        return axes, None
    rgb = "S" in axes and arr.shape[axes.index("S")] in (3, 4)
    if not rgb and n <= MAX_PAGES_AS_CHANNELS:
        return axes.replace(a, "C", 1), "%d pages read as channels" % n
    return axes.replace(a, "Z", 1), "%d pages read as z" % n


def _z_len(arr, axes):
    axes = axes.upper()
    return arr.shape[axes.index("Z")] if "Z" in axes else 1


def _split_axes(arr, axes, projection, z_index):
    """Return (list_of_2D_channel_planes, premerged_rgb_or_None)."""
    axes = axes.upper()

    # 1) collapse any axis that isn't C/Z/Y/X/S by taking the first index (e.g. T)
    for a in [c for c in axes if c not in "CZYXS"]:
        i = axes.index(a)
        arr = np.take(arr, 0, axis=i)
        axes = axes.replace(a, "", 1)

    # 2) RGB samples -> premerged
    if "S" in axes and arr.shape[axes.index("S")] in (3, 4):
        if "Z" in axes:
            arr = _apply_proj(arr, axes.index("Z"), projection, z_index)
            axes = axes.replace("Z", "", 1)
        order = [axes.index("Y"), axes.index("X"), axes.index("S")]
        rgb = _finite(np.transpose(arr, order)[..., :3], "RGB image")
        rgb = rgb / (65535.0 if rgb.max() > 255 else 255.0) if rgb.max() > 1.0 else rgb
        return None, np.clip(rgb, 0.0, 1.0)

    # drop a non-RGB S if present
    if "S" in axes:
        arr = np.take(arr, 0, axis=axes.index("S"))
        axes = axes.replace("S", "", 1)

    # 3) project Z
    if "Z" in axes:
        arr = _apply_proj(arr, axes.index("Z"), projection, z_index)
        axes = axes.replace("Z", "", 1)

    # 4) split channels (Y before X assumed, as in all microscopy TIFF)
    if "C" in axes:
        arr = np.moveaxis(arr, axes.index("C"), 0)
        planes = [np.asarray(arr[i], dtype=np.float32) for i in range(arr.shape[0])]
    else:
        planes = [np.asarray(arr, dtype=np.float32)]
    return planes, None


def _apply_proj(arr, axis, projection, z_index):
    if projection == "mean":
        return arr.mean(axis=axis)
    if projection == "slice":
        i = 0 if z_index is None else int(np.clip(z_index, 0, arr.shape[axis] - 1))
        return np.take(arr, i, axis=axis)
    return arr.max(axis=axis)


# --------------------------------------------------------------------------- #
# OME metadata parsing (names / colours / pixel size)
# --------------------------------------------------------------------------- #
def _ome_color(val):
    try:
        c = int(val) & 0xFFFFFFFF
        r = (c >> 24) & 0xFF
        g = (c >> 16) & 0xFF
        b = (c >> 8) & 0xFF
        if (r, g, b) == (0, 0, 0):
            return None
        return (r / 255.0, g / 255.0, b / 255.0)
    except (TypeError, ValueError):
        return None


def _parse_ome(xml):
    names: List[str] = []
    colors: List = []
    pix = _ome_pixel_size(xml)
    if not xml:
        return names, colors, pix
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        strip = lambda t: t.split("}")[-1]
        for el in root.iter():
            if strip(el.tag) == "Channel":
                names.append(el.get("Name") or "")
                colors.append(_ome_color(el.get("Color")))
    except Exception:
        pass
    return names, colors, pix


def _ome_pixel_size(xml):
    if not xml:
        return None
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        strip = lambda t: t.split("}")[-1]
        for el in root.iter():
            if strip(el.tag) == "Pixels":
                psx = el.get("PhysicalSizeX")
                if not psx:
                    return None
                unit = (el.get("PhysicalSizeXUnit") or "um").lower()
                v = float(psx)
                if unit in ("nm",):
                    return v * 1e-9
                if unit in ("mm",):
                    return v * 1e-3
                return v * 1e-6   # microns (µm / um) default
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
# Leica LIF backend (readlif)
# --------------------------------------------------------------------------- #
def _lif_to_confocal(get_frame: Callable[[int, int], np.ndarray],
                     n_channels: int, n_z: int, projection: str,
                     z_index: Optional[int], pixel_size_m: Optional[float],
                     source: str) -> ConfocalImage:
    """Adapter tested independently of readlif via an injected get_frame(c, z)."""
    channels = []
    for c in range(n_channels):
        frames = [np.asarray(get_frame(c, z)) for z in range(max(n_z, 1))]
        st = np.stack(frames, axis=0)
        plane = _proj_stack(st.astype(np.float32), projection, z_index)
        channels.append(_make_channel(plane, _denom(st.dtype),
                                      "Ch%d" % (c + 1), _palette(c)))
    return ConfocalImage(channels=channels, pixel_size_m=pixel_size_m,
                         source=source, n_z=max(n_z, 1), projection=projection)


def _lif_dims(info) -> dict:
    d = info.get("dims") if isinstance(info, dict) else getattr(info, "dims", None)
    out = {}
    for k in ("x", "y", "z", "t", "m"):
        v = getattr(d, k, None) if d is not None else None
        out[k] = int(v) if isinstance(v, (int, np.integer)) else 1
    return out


def list_lif_images(path: str) -> List[dict]:
    """The images in a Leica .lif: [{index, name, x, y, z, t, m, channels}].
    Reads only the file's header (no pixels)."""
    try:
        from readlif.reader import LifFile
    except ImportError:
        raise ConfocalImportError(
            "Reading Leica .lif needs the 'readlif' package (pip install readlif).")
    lif = LifFile(path)
    out = []
    for i, info in enumerate(getattr(lif, "image_list", []) or []):
        d = _lif_dims(info)
        ch = info.get("channels", 1) if isinstance(info, dict) else 1
        name = info.get("name", "") if isinstance(info, dict) else ""
        out.append(dict(index=i, name=str(name or "image %d" % (i + 1)),
                        channels=int(ch or 1), **d))
    return out


def _from_lif(path: str, projection: str, z_index: Optional[int],
              series: Optional[int] = None) -> ConfocalImage:
    try:
        from readlif.reader import LifFile
    except ImportError:
        raise ConfocalImportError(
            "Reading Leica .lif needs the 'readlif' package (pip install readlif).")
    lif = LifFile(path)
    n_images = len(getattr(lif, "image_list", []) or [])
    idx = 0 if series is None else int(series)
    if n_images and not 0 <= idx < n_images:
        raise ConfocalImportError("%s has %d images; image %d does not exist"
                                  % (path, n_images, idx + 1))
    img = lif.get_image(idx)
    log.info("readlif: %d image(s); opening image %d %r: dims %s, channels %s",
             n_images, idx + 1, getattr(img, "name", "?"), getattr(img, "dims", "?"),
             getattr(img, "channels", "?"))
    nz = getattr(img.dims, "z", 1) or 1
    nc = int(img.channels)
    n_tiles = getattr(img.dims, "m", 1) or 1

    def get_frame(c, z):
        return np.asarray(img.get_frame(z=z, t=0, c=c, m=0))

    pix = None
    try:
        sx = img.scale[0]     # pixels per micron
        if sx:
            pix = (1.0 / float(sx)) * 1e-6
    except Exception:
        pix = None
    ci = _lif_to_confocal(get_frame, nc, nz, projection, z_index, pix, path)
    if n_images > 1:
        ci.notes.append("image %d of %d: %s" % (idx + 1, n_images, getattr(img, "name", "?")))
    if n_tiles > 1:
        ci.notes.append("tile scan of %d tiles: only tile 1 shown (export the merged "
                        "image from LAS X)" % n_tiles)
    return ci
