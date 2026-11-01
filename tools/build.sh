#!/usr/bin/env bash
#
# Builds the installable extension zip into dist/ (nothing is uploaded).
#
#   tools/build.sh
#   BLENDER=/path/to/blender tools/build.sh
set -euo pipefail

cd "$(dirname "$0")/.."
BLENDER="${BLENDER:-}"
if [ -z "$BLENDER" ]; then
  if [ -x /Applications/Blender.app/Contents/MacOS/Blender ]; then
    BLENDER=/Applications/Blender.app/Contents/MacOS/Blender
  else
    BLENDER="$(command -v blender)"
  fi
fi

"$BLENDER" --command extension validate zetta3d
mkdir -p dist
"$BLENDER" --command extension build --source-dir zetta3d --output-dir dist
