# KiCAD JLCPCB tools

Plugin to generate all files necessary for JLCPCB board fabrication and assembly

- Gerber files
- Excellon files
- BOM file
- CPL file

And all that with a simple click on a button 😎

## Future plans

- [x] Generate all neccessary files for JLCPCB PCB production and assembly
- [x] Fetch library data from JLCPCB
- [x] Create interface to select and assign JLCPCB Parts to components

## Warning 🔥

**This plugin is not yet very well tested and only works for KiCAD 5.99 aka nightly builds!**

**This is under a lot of developments, so concider this README out of date all the time 😏**

If you find any sort of problems, please create an issue so that I can hopefully fix it!

## Installation 💾

Simply clone this repo into your scripting/plugins folder, on Windows thats `C:\users\<username>\Documents\kicad\5.99\scripting\plugins\` on linux that would be `/home/<username>/.local/share/kicad/5.99/scripting/plugins`.

## Usage 🥳

Chekout this screencast, it shows quickly how to use this plugin:

![KiCAD JLCPCB example](https://raw.githubusercontent.com/Bouni/kicad-jlcpcb-tools/main/images/showcase.gif)

### Toggle BOM / CPL attributes

You can easily toggle the `exclude from BOM` and `exclude from CPL` attributes of one or multiple footprints.

### Select LCSC parts from the JLCPCB parts database

Select one or multiple footprints, click select part. In the upcoming modal dialog, search for parts, select the one of your choice and click select part.
The LCSC number of your selection will then be assigned to the footprints.

### Generate fabrication data

Generate all neccessary assambly files for your board with a simple click.

A new directory called `jlcpcb` is created, in there two seperate foldes are created, `gerber` and `assembly`.

In the gerber folder all necessary `*.gbr` and `*.drl` files are generated and ziped, ready for upload to JLCPCB.
The zipfile is named `GERBER-<projectname>.zip`

In the assembly folder, two files are generated, `BOM-<projectname>.csv` and `CPL-<projectname>.csv`.

Footprints are included into the BOM and CPL files accordning to their `exclude from BOM` and `exclude from CPL` attributes.
