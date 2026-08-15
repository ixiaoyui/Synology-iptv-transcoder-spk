#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
PKG_STAGE="$BUILD/package"
SPK_STAGE="$BUILD/spk"
OUT="$BUILD/iptv-transcoder-0.1.0-055-x86_64.spk"

rm -rf "$BUILD"
mkdir -p "$PKG_STAGE/lib" "$PKG_STAGE/scripts" "$PKG_STAGE/examples" "$SPK_STAGE"

cp -a "$ROOT/src/iptvtranscoder" "$PKG_STAGE/lib/"
find "$PKG_STAGE/lib" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$PKG_STAGE/lib" -type f -name '*.pyc' -delete
cp -a "$ROOT/examples"/* "$PKG_STAGE/examples/"
cp "$ROOT/README.md" "$PKG_STAGE/README.md"
cp -a "$ROOT/spk/scripts"/* "$PKG_STAGE/scripts/"
find "$PKG_STAGE" -type d -exec chmod 755 {} +
find "$PKG_STAGE" -type f -exec chmod 644 {} +
chmod +x "$PKG_STAGE/scripts"/*

# Synology SPK payload.
tar -C "$PKG_STAGE" -czf "$SPK_STAGE/package.tgz" .
cp "$ROOT/spk/INFO" "$SPK_STAGE/INFO"
cp "$ROOT/spk/PACKAGE_ICON.PNG" "$SPK_STAGE/PACKAGE_ICON.PNG"
cp "$ROOT/spk/PACKAGE_ICON_256.PNG" "$SPK_STAGE/PACKAGE_ICON_256.PNG"
mkdir -p "$SPK_STAGE/scripts"
cp "$ROOT/spk/scripts"/* "$SPK_STAGE/scripts/"
find "$SPK_STAGE" -type d -exec chmod 755 {} +
find "$SPK_STAGE" -type f -exec chmod 644 {} +
chmod +x "$SPK_STAGE/scripts"/*
if [ -d "$ROOT/spk/conf" ] && find "$ROOT/spk/conf" -mindepth 1 -maxdepth 1 | grep -q .; then
  mkdir -p "$SPK_STAGE/conf"
  cp -a "$ROOT/spk/conf"/* "$SPK_STAGE/conf/"
  find "$SPK_STAGE/conf" -type d -exec chmod 755 {} +
  find "$SPK_STAGE/conf" -type f -exec chmod 644 {} +
fi

# DSM SPK is a tar archive containing INFO, package.tgz, scripts/... and optional conf/...
if [ -d "$SPK_STAGE/conf" ] && find "$SPK_STAGE/conf" -mindepth 1 -maxdepth 1 | grep -q .; then
  tar -C "$SPK_STAGE" -cf "$OUT" INFO PACKAGE_ICON.PNG PACKAGE_ICON_256.PNG package.tgz scripts conf
else
  tar -C "$SPK_STAGE" -cf "$OUT" INFO PACKAGE_ICON.PNG PACKAGE_ICON_256.PNG package.tgz scripts
fi
sha256sum "$OUT" > "$OUT.sha256"

echo "$OUT"
cat "$OUT.sha256"
