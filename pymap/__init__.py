"""pyMap - correlative light/electron microscopy registration for pyFIB.

Registers a 2D confocal (moving) image into a FIB (fixed) image's pixel grid
using landmark-based rigid / similarity / affine / thin-plate-spline transforms,
produces a blended overlay for lamella placement in elymaker, and writes CLEM
targets for pyFIB (``pymap.api``).

The FIB image is the milling reference and is NEVER resampled; only the confocal
image is warped into the FIB pixel grid.

Importing ``pymap`` is deliberately light: nothing heavy (scikit-image, tifffile,
Qt) is imported until a name that needs it is used. pyFIB's command-line tools
import ``pymap.api``, which needs only the standard library (plus numpy and Pillow
for ``locate_targets``). The names below stay available as ``pymap.<name>`` and are
loaded on first access.
"""

__version__ = "0.9.2"

# name -> (submodule, attribute). Loaded lazily by __getattr__ (PEP 562, py>=3.7).
_LAZY = {
    "PreAlign": ("model", "PreAlign"),
    "LandmarkPair": ("model", "LandmarkPair"),
    "RegistrationSession": ("model", "RegistrationSession"),
    "TransformKind": ("model", "TransformKind"),
    "FibImageMeta": ("meta", "FibImageMeta"),
    "load_image": ("reader", "load_image"),
    "LoadedImage": ("reader", "LoadedImage"),
    "to_gray": ("reader", "to_gray"),
    "import_confocal": ("confocal", "import_confocal"),
    "ConfocalImage": ("confocal", "ConfocalImage"),
    "ConfocalImportError": ("confocal", "ConfocalImportError"),
    "fit_registration": ("transform", "fit_registration"),
    "Registration": ("transform", "Registration"),
    "RegistrationError": ("transform", "RegistrationError"),
    "ThinPlateSpline": ("tps", "ThinPlateSpline"),
    "apply_prealign": ("warp", "apply_prealign"),
    "warp_moving": ("warp", "warp_moving"),
    "pseudocolor": ("composite", "pseudocolor"),
    "blend_over": ("composite", "blend_over"),
    "COLORMAPS": ("composite", "COLORMAPS"),
    "save_session": ("session", "save_session"),
    "load_session": ("session", "load_session"),
    "write_handoff": ("handoff", "write_handoff"),
    "HandoffJob": ("handoff", "HandoffJob"),
    "HANDOFF_SCHEMA": ("handoff", "HANDOFF_SCHEMA"),
    "register_and_warp": ("pipeline", "register_and_warp"),
    "RegistrationResult": ("pipeline", "RegistrationResult"),
    "registration_qc": ("qc", "registration_qc"),
}

__all__ = ["__version__"] + sorted(_LAZY)


def __getattr__(name):
    try:
        mod_name, attr = _LAZY[name]
    except KeyError:
        raise AttributeError("module 'pymap' has no attribute %r" % name) from None
    import importlib
    value = getattr(importlib.import_module("." + mod_name, __name__), attr)
    globals()[name] = value          # cache: later lookups skip __getattr__
    return value


def __dir__():
    return __all__
