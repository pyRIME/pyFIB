"""Confocal decode: multi-format import -> normalized ConfocalImage.

Isolated so the registration/display core never sees a file format. Backends
(PIL always; tifffile + readlif optional) degrade gracefully.
"""

from .model import Channel, ConfocalImage, DEFAULT_PALETTE, luminance, autoscale
from .importer import import_confocal, ConfocalImportError, PROJECTIONS, list_lif_images

__all__ = [
    "Channel", "ConfocalImage", "DEFAULT_PALETTE", "luminance", "autoscale",
    "import_confocal", "ConfocalImportError", "PROJECTIONS", "list_lif_images",
]
