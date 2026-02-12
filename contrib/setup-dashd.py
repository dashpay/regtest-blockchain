#!/usr/bin/env python3
"""Cross-platform setup script for downloading dashd binaries.

Downloads the Dash Core binary for integration tests.
Outputs DASHD_PATH line suitable for appending to GITHUB_ENV
or evaluating in a shell.

Environment variables:
    DASHVERSION  - Dash Core version (default: 23.0.2)
    CACHE_DIR    - Cache directory (default: ~/.regtest-blockchain-test)
"""

import os
import platform
import sys
import tarfile
import urllib.request
import zipfile

DASHVERSION = os.environ.get("DASHVERSION", "23.0.2")


def get_cache_dir():
    if "CACHE_DIR" in os.environ:
        return os.environ["CACHE_DIR"]
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if not home:
        sys.exit("Cannot determine home directory: neither HOME nor USERPROFILE is set")
    return os.path.join(home, ".regtest-blockchain-test")


def get_asset_filename():
    """Return the asset filename for the current platform."""
    system = platform.system()
    machine = platform.machine()

    if system == "Linux":
        arch = "aarch64" if machine in ("aarch64", "arm64") else "x86_64"
        return f"dashcore-{DASHVERSION}-{arch}-linux-gnu.tar.gz"
    elif system == "Darwin":
        arch = "arm64" if machine == "arm64" else "x86_64"
        return f"dashcore-{DASHVERSION}-{arch}-apple-darwin.tar.gz"
    elif system == "Windows":
        return f"dashcore-{DASHVERSION}-win64.zip"
    else:
        sys.exit(f"Unsupported platform: {system}")


def log(msg):
    print(msg, file=sys.stderr)


def download(url, dest):
    log(f"Downloading {url} ...")
    urllib.request.urlretrieve(url, dest)


def extract(archive_path, dest_dir):
    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(dest_dir)
    else:
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(dest_dir)


def setup_dashd(cache_dir):
    """Download and extract dashd binary. Returns the path to the dashd binary."""
    asset = get_asset_filename()
    dashd_dir = os.path.join(cache_dir, f"dashcore-{DASHVERSION}")

    ext = ".exe" if platform.system() == "Windows" else ""
    dashd_bin = os.path.join(dashd_dir, "bin", f"dashd{ext}")

    if os.path.isfile(dashd_bin):
        log(f"dashd {DASHVERSION} already available at {dashd_bin}")
        return dashd_bin

    log(f"Downloading dashd {DASHVERSION}...")
    archive_path = os.path.join(cache_dir, asset)
    url = f"https://github.com/dashpay/dash/releases/download/v{DASHVERSION}/{asset}"
    download(url, archive_path)
    extract(archive_path, cache_dir)
    os.remove(archive_path)
    log(f"Extracted dashd to {dashd_dir}")

    if not os.path.isfile(dashd_bin):
        sys.exit(f"Expected binary not found after extraction: {dashd_bin}")

    return dashd_bin


def main():
    cache_dir = get_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)

    dashd_path = setup_dashd(cache_dir)

    # Output for GITHUB_ENV or shell eval
    print(f"DASHD_PATH={dashd_path}")


if __name__ == "__main__":
    main()
