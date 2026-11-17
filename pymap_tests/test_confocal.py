import numpy as np
import pytest
from PIL import Image

from pymap.confocal import import_confocal, ConfocalImage, Channel, ConfocalImportError
from pymap.confocal.importer import (_split_axes, _proj_stack, _parse_ome,
                                      _ome_pixel_size, _lif_to_confocal, _ome_color,
                                      _imagej_colors)

tifffile = pytest.importorskip("tifffile")


# ---- model / merge -------------------------------------------------------- #
def test_merge_two_channels_additive():
    g = np.ones((4, 4), np.float32)
    ci = ConfocalImage(channels=[Channel(g, "a", (0, 1, 0)),
                                 Channel(g, "b", (1, 0, 1))])
    assert ci.is_multicolor()
    rgb = ci.merged_rgb().mean(axis=(0, 1))
    # green + magenta -> (1,1,1)
    assert rgb[0] > 0.99 and rgb[1] > 0.99 and rgb[2] > 0.99


def test_single_channel_not_multicolor():
    ci = ConfocalImage(channels=[Channel(np.ones((3, 3), np.float32), "a", (0, 1, 0))])
    assert not ci.is_multicolor()
    assert ci.moving_display().ndim == 2       # gray plane


def test_excluded_channel_not_merged():
    g = np.ones((3, 3), np.float32)
    ci = ConfocalImage(channels=[Channel(g, "a", (0, 1, 0)),
                                 Channel(g, "b", (1, 0, 0), include=False)])
    assert not ci.is_multicolor()               # only 1 included
    assert ci.merged_rgb()[..., 0].max() < 1e-6  # red channel excluded


# ---- projection ----------------------------------------------------------- #
def test_proj_mip_mean_slice():
    stack = np.stack([np.full((2, 2), v, np.float32) for v in (1, 5, 3)], axis=0)
    assert _proj_stack(stack, "mip", None).max() == 5
    assert abs(_proj_stack(stack, "mean", None).mean() - 3.0) < 1e-6
    assert _proj_stack(stack, "slice", 0).max() == 1


# ---- axis splitting ------------------------------------------------------- #
def test_split_zcyx():
    arr = np.zeros((5, 2, 8, 10), np.float32)
    arr[4, 1] = 7.0   # bright in ch1, top z
    chans, pre = _split_axes(arr, "ZCYX", "mip", None)
    assert pre is None and len(chans) == 2
    assert chans[0].shape == (8, 10)
    assert chans[1].max() == 7.0    # mip picked the bright z


def test_split_czyx_and_cyx_and_yx():
    assert len(_split_axes(np.zeros((3, 4, 8, 10)), "CZYX", "mip", None)[0]) == 3
    assert len(_split_axes(np.zeros((2, 8, 10)), "CYX", "mip", None)[0]) == 2
    assert len(_split_axes(np.zeros((8, 10)), "YX", "mip", None)[0]) == 1


def test_split_collapses_time():
    arr = np.zeros((3, 5, 2, 8, 10), np.float32)  # T Z C Y X
    chans, pre = _split_axes(arr, "TZCYX", "mip", None)
    assert pre is None and len(chans) == 2 and chans[0].shape == (8, 10)


def test_split_rgb_samples_premerged():
    arr = np.zeros((8, 10, 3), np.float32)
    arr[..., 0] = 200
    chans, pre = _split_axes(arr, "YXS", "mip", None)
    assert chans is None and pre is not None and pre.shape == (8, 10, 3)
    assert pre[..., 0].max() > 0.5 and pre[..., 1].max() < 1e-6


# ---- real tiff round-trips ------------------------------------------------ #
def test_import_ome_tiff_zcyx(tmp_path):
    p = tmp_path / "s.ome.tif"
    arr = (np.random.rand(6, 3, 20, 24) * 4000).astype(np.uint16)
    tifffile.imwrite(str(p), arr, photometric="minisblack", metadata={"axes": "ZCYX"})
    ci = import_confocal(str(p))
    assert len(ci.channels) == 3
    assert ci.n_z == 6 and ci.projection == "mip"
    assert ci.shape_hw() == (20, 24)
    assert ci.is_multicolor()
    assert 0.0 <= ci.merged_rgb().min() and ci.merged_rgb().max() <= 1.0


def test_import_imagej_hyperstack(tmp_path):
    p = tmp_path / "ij.tif"
    arr = (np.random.rand(4, 2, 16, 20) * 3000).astype(np.uint16)
    tifffile.imwrite(str(p), arr, imagej=True, metadata={"axes": "ZCYX"})
    ci = import_confocal(str(p))
    assert len(ci.channels) == 2 and ci.n_z == 4


def test_import_single_plane_tiff(tmp_path):
    p = tmp_path / "one.tif"
    tifffile.imwrite(str(p), (np.random.rand(16, 16) * 255).astype(np.uint16))
    ci = import_confocal(str(p))
    assert len(ci.channels) == 1 and not ci.is_multicolor()


def test_projection_mean_via_import(tmp_path):
    p = tmp_path / "z.ome.tif"
    arr = np.zeros((3, 1, 8, 8), np.uint16)
    arr[:, 0, 0, 0] = [0, 100, 200]
    tifffile.imwrite(str(p), arr, metadata={"axes": "ZCYX"})
    ci = import_confocal(str(p), projection="mean")
    assert ci.projection == "mean"


# ---- PIL backend ---------------------------------------------------------- #
def test_import_rgb_png_premerged(tmp_path):
    p = tmp_path / "c.png"
    Image.fromarray((np.random.rand(12, 14, 3) * 255).astype(np.uint8)).save(str(p))  # RGB inferred
    ci = import_confocal(str(p))
    assert ci.premerged_rgb is not None and ci.is_multicolor()
    assert ci.moving_display().shape == (12, 14, 3)


def test_import_gray_png_single_channel(tmp_path):
    p = tmp_path / "g.png"
    Image.fromarray((np.random.rand(10, 12) * 255).astype(np.uint8)).save(str(p))
    ci = import_confocal(str(p))
    assert len(ci.channels) == 1 and not ci.is_multicolor()
    assert ci.moving_display().shape == (10, 12)


# ---- OME metadata parsing ------------------------------------------------- #
def test_parse_ome_names_colors_pixelsize():
    xml = (
        '<OME><Image><Pixels PhysicalSizeX="0.09" PhysicalSizeXUnit="um">'
        '<Channel Name="EGFP" Color="16711935"/>'      # green 0x00FF00FF
        '<Channel Name="mCherry" Color="-16776961"/>'   # signed -> red-ish
        '</Pixels></Image></OME>'
    )
    names, colors, pix = _parse_ome(xml)
    assert names == ["EGFP", "mCherry"]
    assert pix == pytest.approx(0.09e-6)
    assert colors[0] is not None


def test_ome_pixel_size_units():
    assert _ome_pixel_size('<OME><Pixels PhysicalSizeX="90" PhysicalSizeXUnit="nm"/></OME>') \
        == pytest.approx(90e-9)


# ---- LIF adapter (fake reader; real .lif bytes untested) ------------------ #
def test_lif_adapter_projects_and_scales():
    def get_frame(c, z):
        a = np.zeros((8, 10), np.float32)
        a[0, 0] = z * 5 + c      # brightest at top z
        return a
    ci = _lif_to_confocal(get_frame, n_channels=2, n_z=3, projection="mip",
                          z_index=None, pixel_size_m=1e-7, source="x.lif")
    assert len(ci.channels) == 2 and ci.n_z == 3
    assert ci.pixel_size_m == 1e-7
    # ch0 mip over z at (0,0) = max(0,5,10)=10 -> autoscaled to 1.0
    assert ci.channels[0].data[0, 0] == pytest.approx(1.0)


def test_lif_missing_package_message(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("readlif"):
            raise ImportError("no readlif")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    from pymap.confocal.importer import _from_lif
    with pytest.raises(ConfocalImportError):
        _from_lif("x.lif", "mip", None)


# ---- per-channel display -------------------------------------------------- #
def test_channel_display_window_brightens():
    data = np.full((4, 4), 0.1, np.float32)
    ch = Channel(data, "c", (0, 1, 0), low=0.0, high=1.0)
    assert ch.display().mean() == pytest.approx(0.1)
    ch.high = 0.2                       # pull the white point down
    assert ch.display().mean() == pytest.approx(0.5)   # 0.1 / 0.2


def test_channel_black_point_clips():
    data = np.linspace(0, 1, 16, dtype=np.float32).reshape(4, 4)
    ch = Channel(data, "c", (1, 1, 1), low=0.5, high=1.0)
    assert ch.display()[0, 0] == 0.0   # below black point


def test_default_window_dim_channel_not_crushed(tmp_path):
    tiff = pytest.importorskip("tifffile")
    # dim channel: low varied signal (0..40) plus a single hot pixel
    arr = np.zeros((1, 2, 40, 40), np.uint8)
    arr[0, 0] = (np.random.rand(40, 40) * 40).astype(np.uint8)
    arr[0, 0, 0, 0] = 255        # one hot pixel
    arr[0, 1] = 200              # bright channel
    p = tmp_path / "d.ome.tif"
    tiff.imwrite(str(p), arr, metadata={"axes": "ZCYX"})
    ci = import_confocal(str(p))
    dim = ci.channels[0]
    assert dim.data.max() == pytest.approx(1.0)     # hot pixel present in true data
    assert dim.high < 0.5                            # p99.5 ignores the single hot px
    assert dim.display().mean() > dim.data.mean()    # so bulk signal is brightened


def test_imagej_luts_give_channel_colors(tmp_path):
    tiff = pytest.importorskip("tifffile")
    from pymap.confocal.importer import _imagej_colors
    ramp = np.arange(256, dtype=np.uint8)
    zero = np.zeros(256, dtype=np.uint8)
    red = np.stack([ramp, zero, zero])
    green = np.stack([zero, ramp, zero])
    colors = _imagej_colors({"LUTs": [red, green]})
    assert colors[0] == pytest.approx((1.0, 0.0, 0.0))
    assert colors[1] == pytest.approx((0.0, 1.0, 0.0))
