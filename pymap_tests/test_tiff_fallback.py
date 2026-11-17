"""TIFF import when tifffile can't decode (LZW/deflate without imagecodecs), and
plain multipage TIFFs (generic page axis) keeping all their pages."""

import numpy as np
import pytest
from PIL import Image

from pymap.confocal import import_confocal, ConfocalImportError
from pymap.confocal import importer

tifffile = pytest.importorskip("tifffile")


def _no_codec(monkeypatch):
    """Make tifffile fail the way it does for LZW without imagecodecs."""
    def boom(self, *a, **k):
        raise ValueError("<COMPRESSION.LZW: 5> requires the 'imagecodecs' package")
    monkeypatch.setattr(tifffile.TiffPageSeries, "asarray", boom)


def _pages(n, h=20, w=30, dtype=np.uint8):
    rng = np.random.default_rng(n)
    hi = 255 if dtype == np.uint8 else 4000
    return [rng.integers(0, hi, (h, w)).astype(dtype) for _ in range(n)]


def _save_pil_pages(path, pages, lzw=False):
    ims = [Image.fromarray(p) for p in pages]
    kw = {"compression": "tiff_lzw"} if lzw else {}
    ims[0].save(path, save_all=True, append_images=ims[1:], **kw)


def test_rgb_lzw_falls_back_to_pillow(tmp_path, monkeypatch):
    rgb = np.random.default_rng(1).integers(0, 255, (24, 32, 3)).astype(np.uint8)
    p = tmp_path / "Maximum_Projection_overlay.tif"
    Image.fromarray(rgb).save(p, compression="tiff_lzw")
    _no_codec(monkeypatch)
    ci = import_confocal(str(p))
    assert ci.premerged_rgb is not None and ci.shape_hw() == (24, 32)
    assert np.allclose(ci.premerged_rgb, rgb / 255.0, atol=1e-6)
    assert "decoded with Pillow" in ci.summary()


def test_plain_two_pages_are_channels(tmp_path):
    pages = _pages(2)
    p = tmp_path / "two.tif"
    _save_pil_pages(p, pages)
    ci = import_confocal(str(p))
    assert len(ci.channels) == 2                    # was 1: the second page was dropped
    assert np.allclose(ci.channels[1].data, pages[1] / 255.0)
    assert "2 pages read as channels" in ci.summary()


def test_plain_two_pages_lzw_fallback_keeps_both(tmp_path, monkeypatch):
    pages = _pages(2, dtype=np.uint16)
    p = tmp_path / "two_lzw.tif"
    _save_pil_pages(p, pages, lzw=True)
    _no_codec(monkeypatch)
    ci = import_confocal(str(p))
    assert len(ci.channels) == 2
    assert np.allclose(ci.channels[0].data, pages[0] / 65535.0)
    assert np.allclose(ci.channels[1].data, pages[1] / 65535.0)


def test_many_plain_pages_are_z(tmp_path):
    pages = _pages(importer.MAX_PAGES_AS_CHANNELS + 2)
    p = tmp_path / "stack.tif"
    _save_pil_pages(p, pages)
    ci = import_confocal(str(p))
    assert len(ci.channels) == 1 and ci.n_z == len(pages)
    assert np.allclose(ci.channels[0].data, np.max(pages, axis=0) / 255.0)


def test_imagej_hyperstack_fallback_keeps_c_and_z(tmp_path, monkeypatch):
    rng = np.random.default_rng(3)
    data = rng.integers(0, 4000, (3, 2, 16, 20)).astype(np.uint16)    # Z C Y X
    p = tmp_path / "hyper.tif"
    tifffile.imwrite(str(p), data, imagej=True, metadata={"axes": "ZCYX"})
    ref = import_confocal(str(p))
    _no_codec(monkeypatch)
    ci = import_confocal(str(p))
    assert len(ci.channels) == len(ref.channels) == 2 and ci.n_z == 3
    for a, b in zip(ci.channels, ref.channels):
        assert np.array_equal(a.data, b.data)


def test_undecodable_tiff_names_imagecodecs(tmp_path, monkeypatch):
    p = tmp_path / "x.tif"
    _save_pil_pages(p, _pages(1), lzw=True)
    _no_codec(monkeypatch)

    def no_pil(*a, **k):
        raise OSError("cannot decode")
    monkeypatch.setattr(importer.Image, "open", no_pil)
    with pytest.raises(ConfocalImportError, match="imagecodecs"):
        import_confocal(str(p))


def test_not_a_tiff_is_an_import_error(tmp_path):
    p = tmp_path / "junk.tif"
    p.write_bytes(b"this is not an image")
    with pytest.raises(ConfocalImportError):
        import_confocal(str(p))
