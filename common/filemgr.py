#!/usr/bin/env python3

"""File management utilities for splitting, reassembling, and downloading files.

This module provides classes to handle:
- Splitting large files into chunks for GitHub compatibility
- Reassembling split files using the split_file_reader package
- Downloading files from GitHub archives
"""

import argparse
from collections.abc import Callable, Generator
import glob
from pathlib import Path
import shutil
import tempfile
from typing import Any
import zipfile

import requests
from split_file_reader.split_file_reader import SplitFileReader
from split_file_reader.split_file_writer import SplitFileWriter

from .progress import NestedProgressBar, NoOpProgressBar, TqdmNestedProgressBar


class FileManager:
    """Manage file splitting, reassembly, and downloads."""

    def __init__(
        self,
        file_path: Path | str,
        chunk_size: int = 80000000,  # 80 MB default
        sentinel_filename: str = "chunk_num.txt",
        compressed_output_file: str | None = None,
        use_temp_dir: bool = False,
    ):
        """Initialize FileManager.

        Args:
            file_path: Path to the file to zip and split.
            chunk_size: Size of each split chunk in bytes (default 80MB for GitHub)
            sentinel_filename: Name of the sentinel file that tracks chunk count.
            compressed_output_file: Path for compressed output file
                                    (defaults to file_path.zip).  May end up being
                                    the prefix of the split files.
            use_temp_dir: If True, create a temporary working directory for
                         intermediate files. Useful for large operations.

        """
        self.file_path = Path(file_path)
        self.chunk_size = chunk_size
        self.sentinel_filename = Path(sentinel_filename)
        self.compressed_output_file = (
            Path(compressed_output_file)
            if compressed_output_file
            else Path(f"{self.file_path}.zip")
        )
        self.use_temp_dir = use_temp_dir
        self.temp_dir: Path | None = None

    def _get_work_dir(self) -> Path:
        """Get the working directory, creating temp dir if needed.

        Returns:
            Path: The working directory (either temp or current).

        """
        if self.use_temp_dir:
            if self.temp_dir is None:
                self.temp_dir = Path(tempfile.mkdtemp(prefix="filemanager_"))
                print(f"Created temporary working directory: {self.temp_dir}")
            return self.temp_dir
        return Path(".")

    def cleanup_temp_dir(self) -> None:
        """Clean up the temporary working directory if it exists.

        This method should be called when finished with the FileManager
        to clean up temporary files.

        """
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)
            print(f"Cleaned up temporary directory: {self.temp_dir}")
            self.temp_dir = None

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit, ensuring temp dir cleanup."""
        self.cleanup_temp_dir()
        return False

    def compress_and_split(
        self, output_dir: Path | None = None, delete_original: bool = False
    ) -> int:
        """Split the file into chunks, creating a sentinel file with chunk count.

        This method maintains compatibility with Generate.split() output format.
        It splits the file at self.file_path into numbered chunks (e.g., file.zip.001, .002, etc.)
        and creates a sentinel file indicating the number of chunks.

        Returns:
            int: The number of chunks created

        Raises:
            FileNotFoundError: If the file to split does not exist.

        """
        if not self.file_path.exists():
            raise FileNotFoundError(f"File to split not found: {self.file_path}")

        class SplitTracker:
            """Custom SplitFileWriter to track chunk count."""

            def __init__(self, file_prefix: str):
                self.file_prefix = file_prefix
                self.files = []

            def gen_split(self) -> Generator[Any, Any, None]:
                while True:
                    name = f"{self.file_prefix}{len(self.files) + 1:03d}"
                    with open(name, "wb") as output_file:
                        self.files.append(name)
                        yield output_file

            def get_chunk_count(self) -> int:
                return len(self.files)

        work_dir = self._get_work_dir()
        print(f"Chunking {self.file_path}")

        # Build output file path in working directory
        # If output_dir is provided, always use it (takes precedence)
        # Otherwise, determine based on compressed_output_file
        if output_dir is not None:
            # Convert output_dir to absolute path to ensure files are created in correct location
            actual_output_dir = Path(output_dir).resolve()
        elif self.compressed_output_file.is_absolute():
            actual_output_dir = self.compressed_output_file.parent
        else:
            actual_output_dir = work_dir / self.compressed_output_file.parent

        actual_output_dir.mkdir(parents=True, exist_ok=True)
        output_prefix = actual_output_dir / self.compressed_output_file.name
        tracker = SplitTracker(str(output_prefix) + ".")
        with (
            SplitFileWriter(tracker.gen_split(), self.chunk_size) as writer,
            zipfile.ZipFile(
                file=writer, mode="w", compression=zipfile.ZIP_DEFLATED
            ) as zip_writer,
        ):
            zip_writer.write(self.file_path, arcname=self.file_path.name)

        # Create sentinel file indicating the number of chunks
        sentinel_path = actual_output_dir / self.sentinel_filename
        with open(sentinel_path, "w", encoding="utf-8") as f:
            f.write(str(tracker.get_chunk_count()))

        print(
            f"Created {tracker.get_chunk_count()} chunks with sentinel file: {sentinel_path}"
        )
        if delete_original:
            self.file_path.unlink()
            print(f"Deleted original file: {self.file_path}")
        return tracker.get_chunk_count()

    def reassemble(
        self,
        output_path: Path | str | None = None,
        input_dir: Path | str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Reassemble split chunks back into the original file.

        Uses split_file_reader to intelligently handle reassembly by reading
        the chunk metadata and combining them in the correct order.

        Args:
            output_path: Path for the reassembled file (defaults to self.file_path)
            input_dir: Directory containing the chunk files (defaults to current directory)
            progress_callback: Optional callback function(bytes_read, total_bytes)
                             for progress reporting

        Returns:
            Path: Path to the reassembled file

        Raises:
            FileNotFoundError: If chunk files are missing
            ValueError: If sentinel file cannot be read or is invalid.

        """
        if output_path is None:
            output_path = self.file_path

        if input_dir is None:
            input_dir = Path(".")
        else:
            input_dir = Path(input_dir)

        output_path = Path(output_path)
        # Search for chunk files in the input directory
        search_pattern = input_dir / f"{self.compressed_output_file.name}*"
        files = sorted(glob.glob(str(search_pattern)))
        print(
            f"Matching chunks with prefix: {search_pattern} got {[Path(f).name for f in files]}"
        )
        print(f"Reassembling {len(files)} chunks into {output_path}")

        # Create a temporary directory to extract the zip contents
        with tempfile.TemporaryDirectory() as temp_extract_dir:
            with (
                SplitFileReader(files, "r") as sfr,
                zipfile.ZipFile(sfr, "r") as zip_reader,  # type: ignore
            ):
                zip_reader.extractall(path=temp_extract_dir)

            # The zip should contain one file - move it to the desired output path
            extracted_files = list(Path(temp_extract_dir).iterdir())
            if len(extracted_files) == 1 and extracted_files[0].is_file():
                # Single file case - move it directly
                shutil.copy2(extracted_files[0], output_path)
            else:
                # Multiple files or directory structure - copy the entire extracted content
                output_path.parent.mkdir(parents=True, exist_ok=True)
                for item in extracted_files:
                    if item.is_file():
                        shutil.copy2(item, output_path.parent / item.name)
                    else:
                        shutil.copytree(
                            item, output_path.parent / item.name, dirs_exist_ok=True
                        )

        print(f"Successfully reassembled file: {output_path}")
        return output_path

    def download(
        self,
        url: str,
        output_path: Path | str | None = None,
        output_dir: Path | str | None = None,
        progress_manager: NestedProgressBar | None = None,
    ) -> Path:
        """Download a file from a URL.

        Supports both simple file downloads and split chunk downloads from GitHub.

        Args:
            url: URL to download (can be file URL or base URL for chunks)
            output_path: Path where the downloaded file should be saved (for simple downloads)
            output_dir: Directory to download chunks into (for chunk downloads)
            progress_manager: Optional NestedProgressBar instance for progress reporting.

        Returns:
            Path: Path to the downloaded file.

        Raises:
            FileNotFoundError: If file cannot be found at the remote location
            OSError: If download fails.

        """
        # Handle simple file download if output_path is provided
        if output_path is not None:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            print(f"Downloading file from {url}")
            try:
                response = requests.get(url, allow_redirects=True, timeout=300)
                response.raise_for_status()
                with open(output_path, "wb") as f:
                    f.write(response.content)
                print(f"Successfully downloaded to {output_path}")
                return output_path
            except requests.RequestException as e:
                raise OSError(f"Failed to download from {url}: {e}") from e

        if progress_manager is None:
            progress_manager = NoOpProgressBar()

        if output_dir is None:
            output_dir = Path(".")
        else:
            output_dir = Path(output_dir)

        # Use temp dir for downloads if configured, otherwise use output_dir
        download_dir = self._get_work_dir() if self.use_temp_dir else output_dir
        download_dir.mkdir(parents=True, exist_ok=True)

        # Download sentinel file first to determine chunk count
        sentinel_url = f"{url}/{self.sentinel_filename}"
        sentinel_local = download_dir / self.sentinel_filename

        print(f"Downloading sentinel file from {sentinel_url}")
        try:
            response = requests.get(sentinel_url, allow_redirects=True, timeout=300)
            response.raise_for_status()
            with open(sentinel_local, "w", encoding="utf-8") as f:
                f.write(response.text)
        except requests.RequestException as e:
            raise FileNotFoundError(
                f"Could not download sentinel file from {sentinel_url}: {e}"
            ) from e

        # Read sentinel to get chunk count
        try:
            with open(sentinel_local, encoding="utf-8") as f:
                chunk_count = int(f.read().strip())
        except ValueError as e:
            raise ValueError(f"Invalid sentinel file format at {sentinel_local}") from e

        print(f"Downloading {chunk_count} chunks from {url}")

        with progress_manager.outer(
            chunk_count,
            description=f"Downloading {self.compressed_output_file.name}    ",
        ) as outer_pbar:  # type: ignore
            for i in range(1, chunk_count + 1):
                chunk_filename = f"{self.compressed_output_file.name}.{i:03d}"
                chunk_url = f"{url}/{chunk_filename}"
                chunk_local = download_dir / chunk_filename

                try:
                    with progress_manager.inner(
                        description=f"Downloading {chunk_filename}"
                    ) as inner_pbar:  # type: ignore
                        response = requests.get(
                            chunk_url,
                            allow_redirects=True,
                            stream=True,
                            timeout=300,
                        )
                        response.raise_for_status()

                        # Get file size from headers
                        file_size = int(response.headers.get("Content-Length", 0))
                        inner_pbar.set_total(file_size)

                        # Download in chunks
                        with open(chunk_local, "wb") as f:
                            for chunk_data in response.iter_content(chunk_size=4096):
                                if chunk_data:
                                    f.write(chunk_data)
                                    inner_pbar.update(len(chunk_data))

                    outer_pbar.update()
                except requests.RequestException as e:
                    raise OSError(
                        f"Failed to download chunk {chunk_filename} from {chunk_url}: {e}"
                    ) from e

        # If using temp dir, copy files to output_dir
        if self.use_temp_dir and download_dir != output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
            for file in download_dir.glob(f"{self.compressed_output_file.name}*"):
                shutil.copy2(file, output_dir / file.name)
            print(f"Copied downloaded files to {output_dir}")

        return output_dir

    def download_and_reassemble(
        self,
        url: str,
        output_path: Path | str | None = None,
        output_dir: Path | str | None = None,
        progress_manager: NestedProgressBar | None = None,
        cleanup: bool = True,
    ) -> Path:
        """Download split chunks from GitHub and reassemble into final file.

        This is a convenience method that combines download() and
        reassemble() into a single workflow, automatically cleaning up all
        intermediate files (chunks and compressed archive).

        Args:
            url: Base URL to the GitHub archive (without filename or chunk extension)
            output_path: Final output filename (defaults to self.file_path)
            output_dir: Directory for chunks and final output file (defaults to current directory)
            progress_manager: Optional NestedProgressBar instance for progress reporting.
            cleanup: If True, delete all intermediate files after reassembly
                    (default: True)

        Returns:
            Path: Path to the final reassembled file (uncompressed)

        Raises:
            FileNotFoundError: If sentinel file cannot be found or chunk files are missing
            ValueError: If sentinel file format is invalid
            OSError: If download or reassembly fails

        """
        if progress_manager is None:
            progress_manager = NoOpProgressBar()

        if output_dir is None:
            output_dir = Path(".")
        else:
            output_dir = Path(output_dir)

        if output_path is None:
            output_path = self.file_path
        else:
            output_path = Path(output_path)

        # Ensure output_path has correct parent directory
        if output_path.parent != output_dir:
            output_path = output_dir / output_path.name

        try:
            # Step 1: Download split files
            print("Starting download and reassemble workflow")
            print(f"  Final output: {output_path}")
            self.download(
                url=url,
                output_dir=output_dir,
                progress_manager=progress_manager,
            )

            # Step 2: Reassemble the downloaded chunks
            print("\nReassembling chunks...")
            reassembled_file = self.reassemble(
                output_path=output_path, input_dir=output_dir
            )

            # Step 3: Clean up intermediate files
            if cleanup:
                self._cleanup_intermediate_files(output_dir)

            print("\n✓ Successfully completed download and reassembly")
            print(f"  Final file: {reassembled_file}")
            print(f"  Size: {reassembled_file.stat().st_size:,} bytes")

            return reassembled_file

        except Exception as e:
            print(f"\n✗ Error during download and reassemble: {e}")
            raise

    def _cleanup_intermediate_files(self, directory: Path | str) -> None:
        """Clean up intermediate chunk and compressed files.

        Removes:
        - Chunk files (e.g., parts-fts5.db.001, .002, etc.)
        - Compressed archive (e.g., parts-fts5.db.zip)
        - Sentinel file (e.g., chunk_num_fts5.txt)

        Args:
            directory: Directory containing intermediate files

        """
        directory = Path(directory)
        files_deleted = 0

        # Delete chunk files
        for chunk_file in directory.glob(f"{self.compressed_output_file.name}.*"):
            try:
                chunk_file.unlink()
                files_deleted += 1
                print(f"  Deleted: {chunk_file.name}")
            except OSError as e:
                print(f"  Warning: Could not delete {chunk_file.name}: {e}")

        # Delete sentinel file
        sentinel_path = directory / self.sentinel_filename
        if sentinel_path.exists():
            try:
                sentinel_path.unlink()
                files_deleted += 1
                print(f"  Deleted: {sentinel_path.name}")
            except OSError as e:
                print(f"  Warning: Could not delete {sentinel_path.name}: {e}")

        print(f"Cleaned up {files_deleted} intermediate files")


def main() -> None:
    """Download parts-fts5.db files from GitHub."""

    parser = argparse.ArgumentParser(
        description="Download parts-fts5.db split files from GitHub release"
    )
    parser.add_argument(
        "-u",
        "--url",
        type=str,
        default="https://bouni.github.io/kicad-jlcpcb-tools/",
        help="Base URL to GitHub release directory containing split files",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("."),
        help="Output directory for downloaded files (default: current directory)",
    )
    parser.add_argument(
        "--reassemble",
        action="store_true",
        default=True,
        help="Reassemble files after downloading",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        help="Output filename for reassembled file (only with --reassemble)",
    )

    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading parts-fts5.db files from {args.url}")
    print(f"Output directory: {output_dir.absolute()}")

    try:
        manager = FileManager(
            file_path="parts-fts5.db", sentinel_filename="chunk_num_fts5.txt"
        )

        # Download and reassemble in a single operation with cleanup
        output_file = args.output_file or (Path(args.output) / "parts-fts5.db")
        manager.download_and_reassemble(
            args.url,
            output_dir=args.output,
            output_path=output_file,
            progress_manager=TqdmNestedProgressBar(),
            cleanup=True,
        )

    except FileNotFoundError as e:
        print(f"\n✗ File not found: {e}")
    except ValueError as e:
        print(f"\n✗ Invalid sentinel file: {e}")
    except OSError as e:
        print(f"\n✗ Download error: {e}")
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")


if __name__ == "__main__":
    main()
