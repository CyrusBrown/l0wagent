#!/bin/bash
# Idempotent system setup, run as root on the first start of a fresh container
# (e.g. after the image is rebuilt). Record every apt install / system change here.
set -euo pipefail

# Example:
# apt-get update && apt-get install -y --no-install-recommends ffmpeg
