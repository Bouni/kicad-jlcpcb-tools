# ![The main window](https://github.com/Bouni/kicad-jlcpcb-tools/raw/main/jlcpcb-icon.png) KiCAD JLCPCB tools

<a href="https://ko-fi.com/I3I364QTM" target="_blank"><img src="https://ko-fi.com/img/githubbutton_sm.svg" height="30px"/></a> <a href="https://www.buymeacoffee.com/bouni" target="_blank"><img src="https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png" height="30px"/></a> <a href="https://github.com/sponsors/Bouni" target="_blank"><img src="https://img.shields.io/badge/-Github Sponsor-fafbfc?style=flat&logo=GitHub%20Sponsors" height="30px"/></a>

***

<img src="https://img.shields.io/badge/KiCAD-v7-green"/> <img src="https://img.shields.io/badge/KiCAD-v8-purple"/> <img src="https://img.shields.io/badge/KiCAD-v8.99-ff69b4"/>

***

[![Update parts database](https://github.com/Bouni/kicad-jlcpcb-tools/actions/workflows/update_parts_database.yml/badge.svg)](https://github.com/Bouni/kicad-jlcpcb-tools/actions/workflows/update_parts_database.yml)

***

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

I try to keep it working with 8.99 nightly builds but there are massive API changes on the horizon and I'm not sure if I can keep up with them.

**This is under a lot of developments, so consider this README out of date all the time 😏**

If you find any sort of problems, please create an issue so that I can hopefully fix it!

## Installation 💾

### KiCAD PCM

Add my custom repo to *the Plugin and Content Manager*, the URL is:

```sh
https://raw.githubusercontent.com/Bouni/bouni-kicad-repository/main/repository.json
```

![image](https://user-images.githubusercontent.com/948965/147682006-9e1dd74a-79d3-492b-a108-15d284acf2b1.png)

From there you can install the plugin via the GUI.

### Git

Simply clone this repo into your `scripting/plugins` folder.

**Windows**

```sh
cd C:\users\<username>\Documents\kicad\<version>\scripting\plugins\  # <username> is your username, <version> can be 7.0, 8.0, or 8.99 depending on the version you use
git clone https://github.com/Bouni/kicad-jlcpcb-tools.git
```

**Linux**

```sh
cd /home/<username>/.local/share/kicad/<version>/scripting/plugins  # <version> can be 7.0, 8.0, or 8.99 depending on the version you use
git clone https://github.com/Bouni/kicad-jlcpcb-tools.git
```

**MacOS**

```sh
cd ~/Library/Preferences/kicad/scripting/plugins
git clone https://github.com/Bouni/kicad-jlcpcb-tools.git
```

You may need to create the `scripting/plugins` folder if it does not exist.

### Flatpack :warning:

The Flatpak installation of KiCAD currently dows not ship with pip and requests installed. The later is required for the plugin to work.
In order to get it working you can run the following 3 commands:

1. `flatpak run --command=sh org.kicad.KiCad`
2. `python -m ensurepip --upgrade`
3. `/var/data/python/bin/pip3 install requests`

See [issue #94](https://github.com/Bouni/kicad-jlcpcb-tools/issues/94) for more info.

## Usage 🥳

To access the plugin choose `Tools → External Plugins → JLCPCB Tools` from the *PCB Editor* menus

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

Generate all necessary assembly files for your board with a simple click.

A new directory called `jlcpcb` is created, and in there, two separate folders are created, `gerber` and `production_files`.

In the gerber folder all necessary `*.gbr` and `*.drl` files are generated and zipped into the `production_files` folder, ready for upload to JLCPCB.
The zipfile is named `GERBER-<projectname>.zip`

Also in the `production_files` folder, two files are generated, `BOM-<projectname>.csv` and `CPL-<projectname>.csv`.

Footprints are included into the BOM and CPL files according to their `exclude from BOM` and `exclude from POS` attributes.

![The fabrication files](https://github.com/Bouni/kicad-jlcpcb-tools/raw/main/images/fabrication_files.png)

## Footprint rotation correction

JLCPCB seems to need corrected rotation information. @matthewlai implemented that in his [JLCKicadTools](https://github.com/matthewlai/JLCKicadTools) and I adopted his work in this plugin as well.
You can download Matthews file from GitHub as well als manage your own corrections in the Rotation manager.

## Icons

This plugin makes use of a lot of icons from the excellent [Material Design Icons](https://materialdesignicons.com/)

## Development

1. Fork repo
2. Git clone forked repo
3. Install pre-commit `pip install pre-commit`
4. Setup pre-commit `pre-commit run`
5. Create feature branch `git switch -c my-awesome-feature`
6. Make your changes
7. Commit your changes `git commit -m "Awesome new feature"`
8. Push to GitHub `git push`
9. Create PR

Make sure you make use of pre-commit hooks in order to format everything nicely with `black`
In the near future I'll add `ruff` / `pylint` and possibly other pre-commit-hooks that enforce nice and clean code style.

## How to rebuild the parts database

The parts database is rebuilt by the [update_parts_database.yml GitHub workflow](https://github.com/Bouni/kicad-jlcpcb-tools/blob/main/.github/workflows/update_parts_database.yml)

You can reference the steps in the 'Update database' section for the commands to run locally.

## Standalone mode

Allows the plugin UI to be started without KiCAD, enabling debugging with an IDE like pycharm / vscode.

Standalone mode is under development.

### Limitations

- All board / footprint / value data are hardcoded stubs, see standalone_impl.py

### How to use

To use the plugin in standlone mode you'll need to identify three pieces of information specific to your Kicad version, plugin path, and OS.

#### Python

The <i><b>{KiCad python}</b></i> should be used, this can be found at different locations depending on your system:

| OS | Kicad python |
|---|---|
|Mac| /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3 |
|Linux| /usr/bin/python3 |
|Windows | C:\Program Files\KiCad\8.0\bin\python.exe |

#### Working directory

The <i><b>{working directory}</b></i> should be your plugins directory, ie:

| OS | Working dir |
|---|---|
|Mac| ~/Documents/KiCad/<version>/scripting/plugins/ |
|Linux| ~/.local/share/kicad/<version>/scripting/plugins/ |
|Windows| %USERPROFILE%\Documents\KiCad\<version>\scripting\plugins\ |

> [!NOTE]  
> <version> can be 7.0, 8.0, or 8.99 depending on the version you use

#### Plugin folder name

The <i><b>{kicad-jlcpcb-tools folder name}</b></i> should be the name of the kicad-jlcpcb-tools folder.

- For Kicad managed plugins this may be like

> com_github_bouni_kicad-jlcpcb-tools

- If you are developing kicad-jlcpcb-tools this is the folder you cloned the kicad-jlcpcb-tools as.

#### Command line

- Change to the working directory as noted above
- Run the python interpreter with the <i><b>{kicad-jlcpcb-tools folder name}</b></i> folder as a module.

For example:

```sh
cd {working directory}
{kicad_python} -m {kicad-jlcpcb-tools folder name}
```

For example on Mac:

```sh
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3 -m kicad-jlcpcb-tools
```

For example on Linux:

```sh
cd ~/.local/share/kicad/8.0/scripting/plugins/ && python -m kicad-jlcpcb-tools
```

For example on Windows:

```cmd
& 'C:\Program Files\KiCad\8.0\bin\python.exe' -m kicad-jlcpcb-tools
```

#### IDE

- Configure the command line to be '{kicad_python} -m {kicad-jlcpcb-tools folder name}'
- Set the working directory to {working directory}

If using PyCharm or Jetbrains IDEs, set the interpreter to Kicad's python, <i><b>{Kicad python}</b></i> and under 'run configuration' select Python.

Click on 'script path' and change instead to 'module name',
entering the name of the kicad-jlcpcb-tools folder, <i><b>{kicad-jlcpcb-tools folder name}</b></i>.
