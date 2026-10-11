#!/usr/bin/env sh
# Rebuild src/tenderbuddy/static/css/app.css from the templates (Tailwind v4 standalone CLI).
# Requires: pip install pytailwindcss   (downloads the standalone binary on first run)
set -eu
cd "$(dirname "$0")/.."
tailwindcss -i assets/tailwind.css -o src/tenderbuddy/static/css/app.css --minify
