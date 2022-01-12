# ![The main window](https://github.com/Bouni/kicad-jlcpcb-tools/raw/main/jlcpcb-icon.png) KiCAD JLCPCB tools

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/I3I364QTM)

Plugin to generate all files necessary for JLCPCB board fabrication and assembly

- Gerber files
- Excellon files
- BOM file
- CPL file

Furthermore it lets you search the JLCPCB parts database and assign parts directly to the footprints which result in them being put into the BOM file.

![The main window](https://github.com/Bouni/kicad-jlcpcb-tools/raw/main/images/main.png)

![The parts library window](https://github.com/Bouni/kicad-jlcpcb-tools/raw/main/images/part_library.png)

![The parts details dialog](https://github.com/Bouni/kicad-jlcpcb-tools/raw/main/images/part_details.png)

## Warning 🔥

**This plugin is not yet very well tested and only works for KiCAD 6.0!**

I try to keep it working with 6.99 nightly builds but there are massive API changes on the horizon and I'm not sure if I can keep up with them.

**This is under a lot of developments, so concider this README out of date all the time 😏**

If you find any sort of problems, please create an issue so that I can hopefully fix it!

## Installation 💾

Add my custom repo to *the Plugin and Content Manager*, the URL is `https://raw.githubusercontent.com/Bouni/bouni-kicad-repository/main/repository.json`

![image](https://user-images.githubusercontent.com/948965/147682006-9e1dd74a-79d3-492b-a108-15d284acf2b1.png)

From there you can install the plugin via the GUI.

**Alternatively:**

Simply clone this repo into your scripting/plugins folder, on Windows thats `C:\users\<username>\Documents\kicad\6.0\scripting\plugins\` on linux that would be `/home/<username>/.local/share/kicad/6.0/scripting/plugins`.

## Usage 🥳

Checkout this screencast, it shows quickly how to use this plugin:

![KiCAD JLCPCB example](https://raw.githubusercontent.com/Bouni/kicad-jlcpcb-tools/main/images/showcase.gif)

## Keyboard shortcuts

Windows can be closed with ctrl-w/ctrl-q/command-w/command-w (OS dependent) and escape.
Pressing enter in the keyword text box will start a search.

### Toggle BOM / CPL attributes

You can easily toggle the `exclude from BOM` and `exclude from CPL` attributes of one or multiple footprints.

### Select LCSC parts from the JLCPCB parts database

Select one or multiple footprints, click select part. You can select parts with equal value and footprint using the Select alike button.
In the upcoming modal dialog, search for parts, select the one of your choice and click select part.
The LCSC number of your selection will then be assigned to the footprints.

![Footprint selection](https://github.com/Bouni/kicad-jlcpcb-tools/raw/main/images/footprint_selection.png)

### Generate fabrication data

Generate all neccessary assambly files for your board with a simple click.

A new directory called `jlcpcb` is created, in there two seperate foldes are created, `gerber` and `assembly`.

In the gerber folder all necessary `*.gbr` and `*.drl` files are generated and ziped, ready for upload to JLCPCB.
The zipfile is named `GERBER-<projectname>.zip`

In the assembly folder, two files are generated, `BOM-<projectname>.csv` and `CPL-<projectname>.csv`.

Footprints are included into the BOM and CPL files accordning to their `exclude from BOM` and `exclude from CPL` attributes.

![The fabrication files](https://github.com/Bouni/kicad-jlcpcb-tools/raw/main/images/fabrication_files.png)

## Footprint rotation correction

JLCPCB seems to need corrected rotation information. @matthewlai implemented that in his [JLCKicadTools](https://github.com/matthewlai/JLCKicadTools) and I adopted his work in this plugin as well.
You can download Matthews file from GitHub as well als manage your own corrections in the Rotation manager.

## Icons

This plugin makes use of a lot of icons from the excelent [Material Design Icons](https://materialdesignicons.com/)
