import os
import sys
import subprocess
import shutil
from pathlib import Path

def find_7z():
    """Find 7z executable."""
    # Check PATH first
    if shutil.which("7z"):
        return "7z"
    
    # Check common Windows locations
    common_paths = [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe"
    ]
    
    for path in common_paths:
        if os.path.exists(path):
            return path
            
    return None

def main():
    print("Checking for 7-Zip...")
    seven_z = find_7z()
    
    if not seven_z:
        print("Error: 7-Zip (7z.exe) not found!")
        print("Please install 7-Zip from https://www.7-zip.org/")
        print("If already installed, ensure it is in your PATH or in the default location.")
        sys.exit(1)
        
    print(f"Found 7-Zip at: {seven_z}")
    
    # Add 7-Zip to PATH for the subprocess if it's not already there
    seven_z_dir = os.path.dirname(seven_z)
    os.environ["PATH"] += os.pathsep + seven_z_dir
    
    # Define paths
    script_path = Path("db_build/jlcparts_db_convert.py")
    if not script_path.exists():
        print(f"Error: Could not find {script_path}")
        sys.exit(1)
        
    print("\nStarting database update (this may take a while)...")
    
    while True:
        response = input("Download fresh database? (y/n): ").lower().strip()
        if response in ['y', 'yes', 'j', 'ja']:
            fetch_arg = ["--fetch-parts-db"]
            print("Downloading and converting JLCPCB database...")
            break
        elif response in ['n', 'no', 'nein']:
            fetch_arg = []
            print("Using existing database in 'db_working/cache.sqlite3'...")
            # Check if file exists to give a better error message
            if not (Path("db_working") / "cache.sqlite3").exists():
                print("Warning: 'db_working/cache.sqlite3' not found. Conversion might fail if no database is present.")
            break
    
    # Run the conversion script
    # We need --skip-cleanup so the .db file is preserved for us to move
    cmd = [sys.executable, str(script_path), "--skip-cleanup"] + fetch_arg
    
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"\nError during database update: {e}")
        sys.exit(1)
        
    # Move the result
    src_db = Path("db_working/parts-fts5.db")
    dest_dir = Path("jlcpcb")
    dest_db = dest_dir / "parts-fts5.db"
    
    if not src_db.exists():
        print(f"\nError: Generated database not found at {src_db}")
        sys.exit(1)
        
    print(f"\nInstalling database to {dest_db}...")
    dest_dir.mkdir(exist_ok=True)
    
    # Backup existing
    if dest_db.exists():
        backup = dest_db.with_suffix(".db.bak")
        if backup.exists():
            backup.unlink()
        dest_db.rename(backup)
        print(f"Backed up existing database to {backup}")
        
    shutil.move(str(src_db), str(dest_db))
    
    print("\nSuccess! Database updated.")
    print("You can now restart KiCad/Plugin to use the new database.")

if __name__ == "__main__":
    main()
