#!/usr/bin/env bash
#
# Builds the extension, installs it into a throwaway Blender user folder (your own Blender setup is never
# touched) and runs tests/smoke.py headless.
#
#   tests/run.sh                       # uses /Applications/Blender.app on macOS, or `blender` on PATH
#   BLENDER=/path/to/blender tests/run.sh
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

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
export BLENDER_USER_RESOURCES="$work/user"

"$BLENDER" --command extension build --source-dir zetta3d --output-dir "$work" >/dev/null
zip="$(ls "$work"/zetta3d-*.zip)"
"$BLENDER" --command extension install-file -r user_default -e "$zip" >/dev/null
"$BLENDER" --background --online-mode --addons bl_ext.user_default.zetta3d \
  --python tests/smoke.py 2>&1 | grep -v "^Read\|^Blender quit\|^$"
