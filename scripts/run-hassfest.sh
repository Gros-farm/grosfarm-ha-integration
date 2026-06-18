#!/usr/bin/env bash
# Run the official Home Assistant hassfest validator against this repo's
# custom_components/. Uses the upstream Docker image so we don't have to
# maintain a parallel Python env that mirrors HA core's dev requirements.
#
# Image: ghcr.io/home-assistant/hassfest:latest
# Entrypoint: walks `find . -name manifest.json` under the mounted workspace
#             and passes each as --integration-path to `python -m script.hassfest`.

set -euo pipefail

cd "$(dirname "$0")/.."

exec docker run --rm \
  -v "$(pwd)/custom_components:/github/workspace/custom_components:ro" \
  -w /github/workspace \
  ghcr.io/home-assistant/hassfest:latest "$@"
