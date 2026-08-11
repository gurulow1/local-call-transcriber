#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$PROJECT_DIR"
[[ -f worker.py && -f scripts/bootstrap_runtime.py ]] || {
  echo "Launcher is not inside an unpacked project folder."
  exit 1
}

mkdir -p logs data/input data/calls data/failed .poetry-cache/bin .poetry-cache/downloads
exec > >(tee -a "$PROJECT_DIR/logs/setup.log") 2>&1

on_error() {
  local status=$?
  trap - ERR
  set +e
  echo
  echo "Setup or launch failed. See logs/setup.log for details."
  if [[ -t 0 ]]; then
    read -r -p "Press Enter to close."
  fi
  exit "$status"
}
trap on_error ERR

UV_VERSION="0.11.13"
case "$(uname -m)" in
  x86_64)
    UV_TARGET="x86_64-unknown-linux-gnu"
    UV_SHA256="f830ea3d38ae1492acf53cb7f2cd0f81d6ae22b42d2d7310a6c7d42c451e1a43"
    ;;
  *)
    echo "Unsupported Linux architecture: $(uname -m). This launcher requires x86_64."
    false
    ;;
esac

for command in curl sha256sum tar mktemp; do
  command -v "$command" >/dev/null || {
    echo "Required system command is missing: $command"
    false
  }
done

UV_BIN="$PROJECT_DIR/.poetry-cache/bin/uv"
if [[ ! -x "$UV_BIN" ]] || [[ "$("$UV_BIN" --version 2>/dev/null || true)" != "uv $UV_VERSION" ]]; then
  ARCHIVE="$PROJECT_DIR/.poetry-cache/downloads/uv-${UV_VERSION}-${UV_TARGET}.tar.gz"
  PARTIAL="${ARCHIVE}.part"
  rm -f "$PARTIAL"
  curl --proto '=https' --tlsv1.2 --fail --location --retry 3 --progress-bar \
    --output "$PARTIAL" \
    "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${UV_TARGET}.tar.gz"
  [[ "$(sha256sum "$PARTIAL" | awk '{print $1}')" == "$UV_SHA256" ]]
  mv "$PARTIAL" "$ARCHIVE"
  STAGE="$(mktemp -d "$PROJECT_DIR/.poetry-cache/uv-stage.XXXXXX")"
  tar -xzf "$ARCHIVE" -C "$STAGE"
  [[ -f "$STAGE/uv-${UV_TARGET}/uv" ]]
  cp "$STAGE/uv-${UV_TARGET}/uv" "$UV_BIN"
  chmod 755 "$UV_BIN"
  rm -r -- "$STAGE"
fi

export UV_CACHE_DIR="$PROJECT_DIR/.poetry-cache/uv-cache"
export UV_PYTHON_INSTALL_DIR="$PROJECT_DIR/.poetry-cache/python"
export UV_NO_CONFIG=1

PYTHON="$PROJECT_DIR/.poetry-cache/venv/bin/python"
if [[ ! -x "$PYTHON" ]] || ! "$PYTHON" -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >/dev/null 2>&1; then
  "$UV_BIN" venv --clear --python 3.12 --managed-python "$PROJECT_DIR/.poetry-cache/venv"
fi

exec "$PYTHON" scripts/bootstrap_runtime.py --uv "$UV_BIN" --profile cpu
