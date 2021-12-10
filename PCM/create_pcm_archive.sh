#!/bin/sh

# heavily inspired by https://github.com/4ms/4ms-kicad-lib/blob/master/PCM/make_archive.sh

VERSION=$1

echo "Clean up old files"
rm -f PCM/*.zip
rm -rf PCM/archive


echo "Create folder structure for ZIP"
mkdir -p PCM/archive/plugins
mkdir -p PCM/archive/resources

echo "Copy files to destination"
cp *.py PCM/archive/plugins
cp *.png PCM/archive/plugins
cp -r icons PCM/archive/plugins
cp PCM/icon.png PCM/archive/resources
cp PCM/metadata.template.json PCM/archive/metadata.json

echo "Modify archive metadata.json"
sed -i "s/VERSION_HERE/$VERSION/g" PCM/archive/metadata.json
sed -i "s/\"kicad_version\": \"6.0\",/\"kicad_version\": \"6.0\"/g" PCM/archive/metadata.json
sed -i "/SHA256_HERE/d" PCM/archive/metadata.json
sed -i "/DOWNLOAD_SIZE_HERE/d" PCM/archive/metadata.json
sed -i "/DOWNLOAD_URL_HERE/d" PCM/archive/metadata.json
sed -i "/INSTALL_SIZE_HERE/d" PCM/archive/metadata.json

echo "Zip PCM archive"
cd PCM/archive
zip -r ../KiCAD-PCM-$VERSION.zip .
cd ../..

echo "Gather data for repo rebuild"
echo VERSION=$VERSION >> $GITHUB_ENV
echo DOWNLOAD_SHA256=$(shasum --algorithm 256 PCM/KiCAD-PCM-$VERSION.zip | xargs | cut -d' ' -f1) >> $GITHUB_ENV
echo DOWNLOAD_SIZE=$(ls -l PCM/KiCAD-PCM-$VERSION.zip | xargs | cut -d' ' -f5) >> $GITHUB_ENV
echo DOWNLOAD_URL="https:\/\/github.com\/Bouni\/kicad-jlcpcb-tools\/releases\/download\/$VERSION\/KiCAD-PCM-$VERSION.zip" >> $GITHUB_ENV
echo INSTALL_SIZE=$(unzip -l PCM/KiCAD-PCM-$VERSION.zip | tail -1 | xargs | cut -d' ' -f1) >> $GITHUB_ENV

