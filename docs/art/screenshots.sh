#!/usr/bin/env bash
#
# Takes the README screenshots in docs/images/ — a real Blender window, a made-up account (see screenshots.py).
# Installs the add-on into a throwaway Blender user folder like tests/run.sh, so your own setup is untouched.
#
#   docs/art/screenshots.sh                  # every shot
#   docs/art/screenshots.sh pipelines        # one
#   BLENDER=/path/to/blender docs/art/screenshots.sh
#
# macOS only (crops with sips). Blender opens a window for each shot and closes it by itself.
set -euo pipefail

cd "$(dirname "$0")/../.."
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
"$BLENDER" --command extension install-file -r user_default -e "$(ls "$work"/zetta3d-*.zip)" >/dev/null
# No splash screen over the window.
"$BLENDER" --background --python-expr \
  "import bpy; v = bpy.context.preferences.view; v.show_splash = False; v.ui_scale = 1.25; bpy.ops.wm.save_userpref()" >/dev/null 2>&1

mkdir -p docs/images
for shot in "${@:-studio rig pipelines agent agent_card}"; do
  for name in $shot; do
    raw="$work/$name.png"
    log="$work/$name.log"
    ZETTA_SHOT="$name" ZETTA_OUT="$raw" "$BLENDER" --online-mode --enable-event-simulate \
      --window-geometry 0 0 1800 1200 --addons bl_ext.user_default.zetta3d \
      --python docs/art/screenshots.py >"$log" 2>&1 &
    pid=$!
    # A shot takes a few seconds; one that hangs is stopped rather than left open.
    for _ in $(seq 90); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill "$pid" 2>/dev/null || true
    grep "^ZETTA_SHOT" "$log" || true
    [ -f "$raw" ] || { tail -30 "$log" >&2; echo "no screenshot for $name" >&2; exit 1; }
    # The studio shot keeps the viewport beside the sidebar; the others are the sidebar alone.
    if [ "$name" = studio ]; then
      cp "$raw" "docs/images/$name.png"
    else
      read -r x w h < <(sed -n 's/^ZETTA_SHOT .* sidebar_x=\([0-9]*\) sidebar_w=\([0-9]*\) area=[0-9]*x\([0-9]*\).*/\1 \2 \3/p' "$log")
      sips --cropToHeightWidth "$h" "$w" --cropOffset 0 "$x" "$raw" --out "docs/images/$name.png" >/dev/null
    fi
  done
done
