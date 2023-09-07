"""
Handles releasing a new version of the plugin.
"""
import sys
from pathlib import Path
import shutil
import json
import hashlib
from datetime import datetime


def _remove_directory_tree(start_directory: Path) -> None:
    """Recursively and permanently removes the specified directory, all of its
    subdirectories, and every file contained in any of those folders."""
    for path in start_directory.iterdir():
        if path.is_file():
            path.unlink()
        else:
            _remove_directory_tree(path)
    start_directory.rmdir()

def _get_directory_size(start_directory: Path) -> int:
    size_sum = 0
    for path in start_directory.iterdir():
        if path.is_file():
            size_sum += path.stat().st_size

        elif path.is_dir():
            size_sum += _get_directory_size(path)

    return size_sum

def _get_sha256(file: Path) -> str:
    with open(file, "rb") as f:
        # Can only be used in Python 3.11
        # zipped_sha256 = hashlib.file_digest(f, "sha256").hexdigest()

        digest = hashlib.sha256()
        digest.update(f.read())

        return digest.hexdigest()

def do_release(version: str) -> None:
    this_file = Path(sys.argv[0]).resolve()
    project_root = this_file.parents[1]
    addons_path = project_root / "addons"

    # Make fresh build directory
    build_path = project_root / "build"
    if build_path.exists():
        assert build_path.is_dir()
        _remove_directory_tree(build_path)
    build_path.mkdir()
    build_src_path = build_path / "src"
    build_src_path.mkdir()

    # Load existing packages and their versions
    packages_src = addons_path / "packages.json"
    assert packages_src.exists()
    assert packages_src.is_file()
    packages_json_target_path = build_path / "packages.json"

    with open(packages_src, "r") as f:
        packages_obj = json.load(f)

    # Process addons
    for addon_path in addons_path.iterdir():
        if addon_path.name in ["packages.json", "repository.json"]:
            continue

        # Addon_path corresponds to an addon ↓
        target_addon_path = build_src_path / addon_path.name

        for item in addon_path.rglob("*"):
            if item.is_dir():
                continue

            relative_bit = item.relative_to(addon_path)
            target_path = target_addon_path / relative_bit
            target_dir = target_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)

            # Copy over regular files
            if item.name != "manifest.json":
                shutil.copyfile(item, target_path)

        # Copy over manifest
        with open(addon_path / "manifest.json", "r") as f:
            manifest = json.load(f)

        # Update version
        manifest["versions"][0]["version"] = version

        with open(target_addon_path / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=2)

        # Zip package
        zip_path_str = str(build_path / (addon_path.name))
        shutil.make_archive(zip_path_str, "zip", target_addon_path)
        zip_path = Path(zip_path_str + ".zip")

        unzipped_size = _get_directory_size(target_addon_path)
        zipped_size = zip_path.stat().st_size
        zipped_sha256 = _get_sha256(zip_path)

        # Add to packages
        # Get index
        idxs_in_packages = []
        for idx, package in enumerate(packages_obj["packages"]):
            if package["identifier"] == manifest["identifier"]:
                idxs_in_packages.append(idx)

        assert len(idxs_in_packages) == 1, "Multiple/No matches!"
        idx_in_packages = idxs_in_packages[0]

        assert len(manifest["versions"]) == 1

        download_url = f"https://github.com/Bouni/kicad-jlcpcb-tools/releases/download/{version}/{zip_path.name}.zip"

        new_version_entry = manifest["versions"][0] | {
            "download_sha256": zipped_sha256,
            "download_size": zipped_size,
            "download_url": download_url,
            "install_size": unzipped_size,
        }

        # Check if new version already exists
        existing_versions = list(entry["version"] for entry in packages_obj["packages"][idx_in_packages]["versions"])
        if new_version_entry["version"] in existing_versions:
            print("Version already present! You can only release a new version.")
            sys.exit(1)

        packages_obj["packages"][idx_in_packages]["versions"].append(new_version_entry)

    # Write packages.json in build directory
    with open(packages_json_target_path, "w") as f:
        json.dump(packages_obj, f, indent=2)

    # Update packages.json in addons directory
    with open(addons_path / "packages.json", "w") as f:
        json.dump(packages_obj, f, indent=2)

    # Write repository.json
    repository_src = addons_path / "repository.json"
    assert repository_src.exists()
    assert repository_src.is_file()
    repository_json_target_path = build_path / "repository.json"

    with open(repository_src, "r") as f:
        repository_obj = json.load(f)

    update_time = datetime.utcnow()
    repository_packages = {
        "sha256": _get_sha256(packages_json_target_path),
        "update_time_utc": update_time.strftime("%Y-%m-%d %H:%M:%S"),
        "update_timestamp": round(update_time.timestamp()),
        "url": "https://github.com/Bouni/kicad-jlcpcb-tools/releases/download/{version}/repository.json",
    }

    repository_obj["packages"] = repository_packages

    with open(repository_json_target_path, "w") as f:
        json.dump(repository_obj, f, indent=2)


def exit_help() -> None:
    print("Usage: python release.py VERSION")

    sys.exit(1)

def main():
    if len(sys.argv) != 2:
        exit_help()

    version = sys.argv[1]

    do_release(version)
    print("Built release. See output in ./build")

    sys.exit(0)

if __name__ == "__main__":
    main()
