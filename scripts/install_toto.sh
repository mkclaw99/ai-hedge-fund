#!/usr/bin/env bash
# Install Datadog Toto-2.0 as an optional extra for the Time Series
# Forecaster (Toto) analyst. Not added to pyproject.toml because:
#
#   1. `dd-unit-scaling` and `toto-2` are both in the same DataDog/toto
#      repo (different subdirectories). Poetry's git+subdirectory resolver
#      reads the repo-root `pyproject.toml` (for `toto-ts`, Toto 1.0) and
#      rejects the package name. Pip handles the subdirectory correctly.
#
#   2. Toto-2.0 is heavy (1.2 GB weights + ~500 MB of transitive deps:
#      gluonts, lightning, jaxtyping). Making it optional keeps the
#      default `poetry install` lean for users who only want Chronos-2.
#
#   3. Apple Silicon MPS is broken for Toto (Metal's MPSNDArraySort kernel
#      only supports axes 0-3; Toto's forecast call uses axis 4). CPU
#      works fine — sub-100ms per forecast on M-series — but users on
#      CUDA-less Linux boxes might want to skip.
#
# Run after the standard `poetry install`. Idempotent: re-running upgrades
# to the latest commit on DataDog/toto:main.

set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v poetry >/dev/null 2>&1; then
  echo "poetry not found on PATH. Install poetry first." >&2
  exit 1
fi

# Check Python version — Toto requires 3.12+. The hedge fund's pyproject
# pins ^3.12, so a freshly-installed venv will already be on 3.12; this
# is a belt-and-braces check for users who upgraded an older venv.
PY_VER=$(poetry run python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if ! poetry run python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"; then
  echo "Toto-2.0 requires Python >= 3.12 (your venv is on ${PY_VER})." >&2
  echo "Run: poetry env use python3.12 && poetry install" >&2
  exit 1
fi

echo "Installing dd-unit-scaling..."
poetry run pip install \
  "dd-unit-scaling @ git+https://github.com/DataDog/toto.git#subdirectory=dd_unit_scaling"

echo "Installing toto-2..."
poetry run pip install \
  "toto-2 @ git+https://github.com/DataDog/toto.git#subdirectory=toto2"

echo "Smoke-testing import..."
poetry run python -c "from toto2 import Toto2Model; print('Toto2Model imports OK')"

echo
echo "Done. The Toto Forecaster analyst is now available in the agents list."
echo "First forecast downloads the model (~1.2 GB) into ~/.cache/huggingface."
