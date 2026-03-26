"""SessionFS — Portable AI coding sessions."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("sessionfs")
except PackageNotFoundError:
    __version__ = "0.7.1"  # fallback for development
