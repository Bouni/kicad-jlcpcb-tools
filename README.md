# KiCAD JLCPCB tools

Plugin to generate all files necessary for JLCPCB board fabrication and assembly

 - Gerber files
 - Excellon files
 - BOM file
 - CPL file

And all that with a simple click on a button 😎

## Warning 🔥

**This plugin is not yet very well tested and only works for KiCAD 5.99 aka nightly builds!**

If you find any sort of problems, please create an issue so that I can hopefully fix it!

## Installation 💾

Simply clone this repo into your scripting/plugins folder, on Windows thats `C:\users\<username>\Documents\kicad\5.99\scripting\plugins\` on linux that would be `/home/<username>/.local/share/kicad/5.99/scripting/plugins`.

## Usage 🥳

Open up your board in `pcbnew` where you should see a shiny new JLCPCB button

![KiCAD Toolbar JLCPCB button](https://raw.githubusercontent.com/Bouni/kicad-jlcpcb-tools/main/images/toolbar.png)

Simply click te button and your files are geberated in your project folder.
A new directory called `jlcpcb` is created, in there two seperate foldes are created, `gerber` and `assembly`.

In the gerber folder all necessary `*.gbr` and `*.drl` files are generated and ziped, ready for upload to JLCPCB.
The zipfile is named `GERBER-<projectname>.zip`

In the assembly folder, two files are generated, `BOM-<projectname>.csv` and `CPL-<projectname>.csv`.

To exclude footprints from the assembly files, you can select if they should be excluded from BOM and/or CPL by checking the checkboxes in the lower right corner.

![KiCAD exclude from BOM or CPL](https://raw.githubusercontent.com/Bouni/kicad-jlcpcb-tools/main/images/exclude.png)
