#!/bin/sh

# heavily inspired by https://github.com/4ms/4ms-kicad-lib/blob/master/PCM/make_archive.sh

VERSION="2021.12.01"

echo "Clean up old zip files"
rm -f PCM/*.zip

echo "Create folder structure for ZIP"
mkdir -p PCM/archive/{plugin,resources}

echo "Copy files to destination"
cp *.py PCM/archive/plugins
cp *.png PCM/archive/plugins
cp -r icons PCM/archive/plugins
cp PCM/logo.png PCM/archive/resources
cp PCM/metadata.template.json PCM/archive/metadata.json
cp PCM/metadata.template.json PCM/metadata.json

echo "Modify archive metadata.json"
sed -i 's/VERSION_HERE/$VESRION/g' PCM/archive/metadata.json
sed -i '/SHA256_HERE/d' PCM/archive/metadata.json
sed -i '/DOWNLOAD_SIZE_HERE/d' PCM/archive/metadata.json
sed -i '/DOWNLOAD_URL_HERE/d' PCM/archive/metadata.json
sed -i '/INSTALL_SIZE_HERE/d' PCM/archive/metadata.json

echo "Zip PCM archive"
cd PCM/archive
zip -r ../KiCAD-PCM-$VERSION.zip .
cd ../..

echo "Gather data for merge request metadata.json"
DOWNLOAD_SHA256=$(shasum --algorithm 256 PCM/KiCAD-PCM-$VERSION.zip | xargs | cut -d' ' -f1)
DOWNLOAD_SIZE=$(ls -l PCM/KiCAD-PCM-$VERSION.zip | xargs | cut -d' ' -f5)
INSTALL_SIZE=$(unzip -l PCM/KiCAD-PCM-$VERSION.zip | tail -1 | xargs | cut -d' ' -f1)

echo "Modify merge request metadata.json"
sed -i 's/VERSION_HERE/$VESRION/g' PCM/metadata.json
sed -i 's/SHA256_HERE/$DOWNLOAD_SHA256/g' PCM/metadata.json
sed -i 's/DOWNLOAD_SIZE_HERE/$DOWNLOAD_SIZE/g' PCM/metadata.json
sed -i 's/DOWNLOAD_URL_HERE/ToDo/g' PCM/metadata.json
sed -i 's/INSTALL_SIZE_HERE/$INSTALL_SIZE/g' PCM/metadata.json
