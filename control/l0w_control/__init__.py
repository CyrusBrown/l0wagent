import os

# The release image sets L0W_VERSION (the git tag, or "<latest tag>-edge.<sha>" on the edge channel); development reports the base version.
__version__ = os.environ.get("L0W_VERSION", "0.5.0-dev")
