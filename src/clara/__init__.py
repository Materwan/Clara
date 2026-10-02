"""Clara: one AI, one memory, many clients."""

from importlib import metadata

try:
    __version__ = metadata.version("clara-server")  # pyproject.toml is the one place the version is written
except metadata.PackageNotFoundError:  # running from a plain checkout, not installed
    __version__ = "0+unknown"
