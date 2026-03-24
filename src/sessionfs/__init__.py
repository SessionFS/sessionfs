"""SessionFS — Portable AI coding sessions."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("sessionfs")
except PackageNotFoundError:
    __version__ = "0.4.0"  # fallback for development
