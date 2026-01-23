"""Test FileManager split and reassemble workflow."""

from http.server import HTTPServer, SimpleHTTPRequestHandler
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

import pytest

from common.filemgr import FileManager


@pytest.fixture
def temp_test_dir():
    """Create a temporary test directory and clean up after test.

    This fixture creates a unique temporary directory for each test,
    allowing tests to safely create and modify files without affecting
    other tests or the system. The directory and all its contents are
    automatically deleted after the test completes, even if the test fails.

    All test output files should be created within this directory to ensure
    proper cleanup.

    Returns:
        Path: The temporary directory path.

    """
    test_dir = Path(tempfile.mkdtemp(prefix="test_filemgr_"))
    yield test_dir
    shutil.rmtree(test_dir, ignore_errors=True)


@pytest.fixture
def temp_file(temp_test_dir):
    """Create a temporary test file with content.

    Creates a test file in the temporary test directory with predefined content.
    The file is automatically cleaned up when temp_test_dir is cleaned up.

    Args:
        temp_test_dir: The temporary test directory fixture.

    Returns:
        tuple: (test_file_path, test_content) for assertions and verification.

    """
    test_file = temp_test_dir / "test_data.txt"
    test_content = "Test content " * 10000
    test_file.write_text(test_content)
    return test_file, test_content


@pytest.fixture
def http_server(temp_test_dir):
    """Start a temporary HTTP server serving files from temp_test_dir.

    This fixture creates a local HTTP server that serves files from the
    temporary test directory. The server runs in a background thread and
    is automatically shut down after the test completes.

    The temp_test_dir is automatically cleaned up after the fixture exits,
    which ensures all test files are removed.

    Yields:
        tuple: (server_url, temp_test_dir) where server_url is the base URL
               (e.g., http://127.0.0.1:12345) to use for requests.

    """

    class QuietHTTPRequestHandler(SimpleHTTPRequestHandler):
        """HTTP request handler that suppresses logging."""

        def log_message(self, format, *args):
            pass  # Suppress server logs during tests

        def translate_path(self, path):
            """Override to serve from temp_test_dir."""
            # Simplified: just map the request path directly to temp_test_dir
            path = path.split("?", 1)[0]  # Remove query string
            path = path.split("#", 1)[0]  # Remove fragment
            words = path.split("/")
            words = filter(None, words)  # Remove empty strings
            path = temp_test_dir
            for word in words:
                if word in (os.curdir, os.pardir):
                    continue
                path = path / word
            return str(path)

    server = HTTPServer(("localhost", 0), QuietHTTPRequestHandler)
    host, port = server.server_address[:2]
    server_url = f"http://{host}:{port}"

    # Start server in background thread
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # Give server time to start
    time.sleep(0.1)

    yield server_url, temp_test_dir

    # Shutdown server
    server.shutdown()
    server.server_close()


# ============================================================================
# Basic Tests (existing)
# ============================================================================


def test_split_and_reassemble(temp_file, temp_test_dir):
    """Test the split and reassemble workflow."""
    test_file, test_content = temp_file
    output_dir = temp_test_dir / "output"
    output_dir.mkdir()

    # Test the split operation
    fm = FileManager(
        file_path=test_file,
        chunk_size=5000,  # Small chunks for testing
        sentinel_filename="test_chunks.txt",
    )

    chunk_count = fm.compress_and_split(output_dir=output_dir)
    assert chunk_count > 0, "Should create at least one chunk"

    # Verify chunk files exist
    chunk_files = list(output_dir.glob("test_data.txt.zip.*"))
    assert len(chunk_files) == chunk_count, (
        "Number of chunk files should match chunk count"
    )

    # Verify sentinel file exists
    sentinel_file = output_dir / "test_chunks.txt"
    assert sentinel_file.exists(), "Sentinel file should exist"
    assert int(sentinel_file.read_text()) == chunk_count, (
        "Sentinel should contain chunk count"
    )

    # Test reassemble
    reassembled_file = output_dir / "test_data_reassembled.txt"
    reassembled_path = fm.reassemble(output_path=reassembled_file, input_dir=output_dir)

    # Verify reassembled file
    assert reassembled_path.exists(), "Reassembled file should exist"
    reassembled_content = reassembled_path.read_text()
    assert reassembled_content == test_content, "Content should match original"


def test_compress_and_split(temp_test_dir):
    """Test the compress_and_split operation."""
    test_file = temp_test_dir / "test_file.txt"
    test_file.write_text("Test content " * 5000)

    output_dir = temp_test_dir / "output"
    output_dir.mkdir()

    fm = FileManager(
        file_path=test_file,
        chunk_size=8000,
        sentinel_filename="chunk_count.txt",
    )

    chunk_count = fm.compress_and_split(output_dir=output_dir, delete_original=True)

    # Verify original file was deleted
    assert not test_file.exists(), (
        "Original file should be deleted when delete_original=True"
    )

    # Verify chunks were created
    chunk_files = sorted(output_dir.glob("test_file.txt.zip.*"))
    assert len(chunk_files) == chunk_count, "All chunks should be created"


def test_temp_dir_context_manager(temp_test_dir):
    """Test temporary working directory feature with context manager."""
    test_file = temp_test_dir / "test_file.txt"
    test_file.write_text("Hello World" * 1000)

    # Test with use_temp_dir=True
    with FileManager(test_file, use_temp_dir=True) as fm:
        assert fm.use_temp_dir is True, "use_temp_dir should be True"
        work_dir = fm._get_work_dir()
        assert work_dir.exists(), "Work directory should exist"
        temp_dir_path = fm.temp_dir
        assert temp_dir_path is not None, "Temp dir should be created"

    # After context exit, temp dir should be cleaned up
    assert fm.temp_dir is None, "Temp dir should be None after context exit"
    assert not temp_dir_path.exists(), "Temp dir should be deleted after context exit"


def test_temp_dir_without_context_manager(temp_test_dir):
    """Test temporary working directory without context manager."""
    test_file = temp_test_dir / "test_file.txt"
    test_file.write_text("Hello World" * 1000)

    fm = FileManager(test_file, use_temp_dir=True)
    work_dir = fm._get_work_dir()
    assert work_dir.exists(), "Work directory should exist"

    temp_dir_path = fm.temp_dir
    assert temp_dir_path is not None, "Temp dir should be created"

    # Manually cleanup
    fm.cleanup_temp_dir()
    assert fm.temp_dir is None, "Temp dir should be None after cleanup"
    assert not temp_dir_path.exists(), "Temp dir should be deleted after cleanup"


def test_no_temp_dir(temp_test_dir):
    """Test FileManager with use_temp_dir=False."""
    test_file = temp_test_dir / "test_file.txt"
    test_file.write_text("Hello World" * 1000)

    fm = FileManager(test_file, use_temp_dir=False)
    work_dir = fm._get_work_dir()
    assert work_dir == Path("."), "Work directory should be current directory"
    assert fm.temp_dir is None, "Temp dir should be None when use_temp_dir=False"


# ============================================================================
# End-to-End Tests with HTTP Server
# ============================================================================


def test_download_single_file(http_server, temp_test_dir):
    """Test downloading a single file from GitHub."""
    server_url, serve_dir = http_server

    # Create a test file in the server directory
    test_file = serve_dir / "download_test.txt"
    test_content = "Downloaded file content"
    test_file.write_text(test_content)

    # Download the file
    download_dir = temp_test_dir / "downloads"
    download_dir.mkdir()

    fm = FileManager(
        file_path=download_dir / "download_test.txt",
        use_temp_dir=False,
    )
    downloaded_path = fm.download(
        url=f"{server_url}/download_test.txt",
        output_path=download_dir / "download_test.txt",
    )

    assert downloaded_path.exists(), "Downloaded file should exist"
    assert downloaded_path.read_text() == test_content, (
        "Downloaded content should match"
    )


def test_download_and_reassemble_split_chunks(http_server, temp_test_dir):
    """Test downloading split chunks and reassembling them."""
    server_url, serve_dir = http_server

    # Create original file and split it
    original_file = serve_dir / "original.txt"
    original_content = "Original file content " * 5000
    original_file.write_text(original_content)

    # Split the file
    split_output_dir = serve_dir / "chunks"
    split_output_dir.mkdir()

    fm = FileManager(
        file_path=original_file,
        chunk_size=3000,
        sentinel_filename="chunk_count.txt",
    )
    fm.compress_and_split(output_dir=split_output_dir)

    # Now download and reassemble
    download_dir = temp_test_dir / "downloads"
    download_dir.mkdir()

    fm_download = FileManager(
        file_path=download_dir / "original.txt",
        sentinel_filename="chunk_count.txt",
        use_temp_dir=False,
    )

    reassembled_path = fm_download.download_and_reassemble(
        url=f"{server_url}/chunks/",
        output_dir=download_dir,
        output_path=download_dir / "reassembled.txt",
    )

    assert reassembled_path.exists(), "Reassembled file should exist"
    assert reassembled_path.read_text() == original_content, (
        "Reassembled content should match original"
    )


def test_download_and_reassemble_with_multiple_chunks(http_server, temp_test_dir):
    """Test download_and_reassemble with many chunks."""
    server_url, serve_dir = http_server

    # Create a larger file that will be split into many chunks
    original_file = serve_dir / "large.txt"
    original_content = (
        "Large file content " * 100000
    )  # Much larger to ensure multiple chunks
    original_file.write_text(original_content)

    # Split into small chunks
    split_output_dir = serve_dir / "large_chunks"
    split_output_dir.mkdir()

    fm = FileManager(
        file_path=original_file,
        chunk_size=1000,  # Smaller chunks to ensure multiple
        sentinel_filename="large_chunk_count.txt",
    )
    chunk_count = fm.compress_and_split(output_dir=split_output_dir)
    assert chunk_count > 3, "Should create multiple chunks for this test"

    # Download and reassemble
    download_dir = temp_test_dir / "downloads"
    download_dir.mkdir()

    fm_download = FileManager(
        file_path=download_dir / "large.txt",
        sentinel_filename="large_chunk_count.txt",
        use_temp_dir=False,
    )

    reassembled_path = fm_download.download_and_reassemble(
        url=f"{server_url}/large_chunks/",
        output_dir=download_dir,
        output_path=download_dir / "large_reassembled.txt",
    )

    assert reassembled_path.exists(), "Reassembled file should exist"
    assert reassembled_path.read_text() == original_content, (
        "Reassembled content should match original"
    )
    assert len(list(download_dir.glob("large.txt.zip.*"))) == 0, (
        "Downloaded chunks should be cleaned up after reassembly"
    )


def test_download_missing_sentinel_file(http_server, temp_test_dir):
    """Test download_and_reassemble handles missing sentinel file gracefully."""
    server_url, serve_dir = http_server

    # Create chunks directory without sentinel file
    chunks_dir = serve_dir / "no_sentinel"
    chunks_dir.mkdir()

    download_dir = temp_test_dir / "downloads"
    download_dir.mkdir()

    fm_download = FileManager(
        file_path=download_dir / "test.txt",
        sentinel_filename="chunk_count.txt",
        use_temp_dir=False,
    )

    # Should raise an error or handle gracefully
    with pytest.raises((FileNotFoundError, Exception)):
        fm_download.download_and_reassemble(
            url=f"{server_url}/no_sentinel/",
            output_dir=download_dir,
        )


def test_download_partial_chunks(http_server, temp_test_dir):
    """Test download_and_reassemble handles incomplete chunk sets."""
    server_url, serve_dir = http_server

    # Create chunks directory with only some chunks
    original_file = serve_dir / "partial.txt"
    original_content = "Partial file " * 50000  # Much larger to ensure multiple chunks
    original_file.write_text(original_content)

    chunks_dir = serve_dir / "partial_chunks"
    chunks_dir.mkdir()

    fm = FileManager(
        file_path=original_file,
        chunk_size=1000,  # Smaller to ensure multiple chunks
        sentinel_filename="partial_count.txt",
    )
    chunk_count = fm.compress_and_split(output_dir=chunks_dir)
    assert chunk_count > 1, "Should create multiple chunks for this test"

    # Remove some chunks to simulate incomplete download
    chunk_files = sorted(chunks_dir.glob("partial.txt.zip.*"))
    if len(chunk_files) > 1:
        chunk_files[-1].unlink()

    download_dir = temp_test_dir / "downloads"
    download_dir.mkdir()

    fm_download = FileManager(
        file_path=download_dir / "partial.txt",
        sentinel_filename="partial_count.txt",
        use_temp_dir=False,
    )

    # Should raise an error due to missing chunk
    with pytest.raises((FileNotFoundError, Exception)):
        fm_download.download_and_reassemble(
            url=f"{server_url}/partial_chunks/",
            output_dir=download_dir,
            output_path=download_dir / "partial_reassembled.txt",
        )
