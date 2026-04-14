#!/bin/sh

# heavily inspired by https://github.com/4ms/4ms-kicad-lib/blob/master/PCM/make_archive.sh
set -eu

if [ $# -lt 1 ]; then
	echo "Usage: $0 <version>"
	exit 1
fi

VERSION=$1
ARCHIVE_DIR="PCM/archive"
PLUGINS_DIR="$ARCHIVE_DIR/plugins"
RESOURCES_DIR="$ARCHIVE_DIR/resources"
ZIP_FILE="PCM/KiCAD-PCM-$VERSION.zip"
METADATA_FILE="$ARCHIVE_DIR/metadata.json"

sed_inplace() {
	sed -i.bak "$1" "$2"
	rm -f "$2.bak"
}

echo "Clean up old files"
rm -f PCM/*.zip
rm -rf "$ARCHIVE_DIR"

echo "Create folder structure for ZIP"
mkdir -p "$PLUGINS_DIR" "$RESOURCES_DIR"

echo "Copy top-level files"
for file in VERSION settings.json ./*.py ./*.png; do
	[ -e "$file" ] || continue
	cp "$file" "$PLUGINS_DIR"
done

echo "Copy directories"
for dir in icons lib common dblib core; do
	cp -R "$dir" "$PLUGINS_DIR"
done

echo "Prune tests and caches from packaged plugin"
find "$PLUGINS_DIR/common" "$PLUGINS_DIR/dblib" "$PLUGINS_DIR/core" -type f \( -name 'test_*.py' -o -name 'pytest.ini' \) -delete
find "$PLUGINS_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$PLUGINS_DIR" -type f -name '*.pyc' -delete

cp PCM/icon.png "$RESOURCES_DIR"
cp PCM/metadata.template.json "$METADATA_FILE"

echo "Write version info to file"
echo "$VERSION" > "$PLUGINS_DIR/VERSION"

echo "Modify archive metadata.json"
sed_inplace "s/VERSION_HERE/$VERSION/g" "$METADATA_FILE"
sed_inplace "s/\"kicad_version\": \"6.0\",/\"kicad_version\": \"6.0\"/g" "$METADATA_FILE"
for placeholder in SHA256_HERE DOWNLOAD_SIZE_HERE DOWNLOAD_URL_HERE INSTALL_SIZE_HERE; do
	sed_inplace "/$placeholder/d" "$METADATA_FILE"
done

echo "Zip PCM archive"
(cd "$ARCHIVE_DIR" && zip -r "../KiCAD-PCM-$VERSION.zip" .)

echo "Gather data for repo rebuild"
DOWNLOAD_SHA256=$(shasum --algorithm 256 "$ZIP_FILE" | awk '{print $1}')
DOWNLOAD_SIZE=$(wc -c < "$ZIP_FILE" | tr -d '[:space:]')
DOWNLOAD_URL="https:\/\/github.com\/Bouni\/kicad-jlcpcb-tools\/releases\/download\/$VERSION\/KiCAD-PCM-$VERSION.zip"
INSTALL_SIZE=$(unzip -l "$ZIP_FILE" | awk 'END{print $1}')

if [ -n "${GITHUB_ENV:-}" ]; then
	echo "VERSION=$VERSION" >> "$GITHUB_ENV"
	echo "DOWNLOAD_SHA256=$DOWNLOAD_SHA256" >> "$GITHUB_ENV"
	echo "DOWNLOAD_SIZE=$DOWNLOAD_SIZE" >> "$GITHUB_ENV"
	echo "DOWNLOAD_URL=$DOWNLOAD_URL" >> "$GITHUB_ENV"
	echo "INSTALL_SIZE=$INSTALL_SIZE" >> "$GITHUB_ENV"
else
	echo "VERSION=$VERSION"
	echo "DOWNLOAD_SHA256=$DOWNLOAD_SHA256"
	echo "DOWNLOAD_SIZE=$DOWNLOAD_SIZE"
	echo "DOWNLOAD_URL=$DOWNLOAD_URL"
	echo "INSTALL_SIZE=$INSTALL_SIZE"
fi

