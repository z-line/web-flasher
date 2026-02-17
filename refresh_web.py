#!/usr/bin/env python3
"""Combined refresh and artifact fetch script converted from shell to Python3.

Usage: run from repository root or call `./refresh_web.sh` which wraps this.
"""
import sys
import os
import shutil
import subprocess
import urllib.request
import urllib.error
import json
import zipfile
import tempfile
from pathlib import Path
from enum import Enum
import re
import time


def run(cmd, cwd=None, capture=False, check=False, stdin=None):
    if capture:
        res = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            stdin=stdin,
        )
        if check and res.returncode != 0:
            raise subprocess.CalledProcessError(res.returncode, cmd, res.stdout)
        return res.returncode, res.stdout
    else:
        return subprocess.run(cmd, cwd=cwd, stdin=stdin).returncode, None


HERE = Path(__file__).resolve().parent
WEB_SOURCE_DIR = HERE
TARGETS_DIR = HERE.parent / "ExpressLRSTargets"

# Network retry configuration
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds
TIMEOUT = 30  # seconds


def fetch_with_retry(url: str, timeout: int = TIMEOUT, max_retries: int = MAX_RETRIES):
    """Fetch URL with retry mechanism and exponential backoff."""
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            if attempt < max_retries - 1:
                delay = RETRY_DELAY * (2**attempt)  # exponential backoff
                print(
                    f"Retry {attempt + 1}/{max_retries} for {url} after {delay}s (error: {e})"
                )
                time.sleep(delay)
            else:
                print(f"Failed to fetch {url} after {max_retries} attempts: {e}")
                raise
    return None


def fetch_url_to(path: Path, url: str):
    """Download file with retry mechanism."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = fetch_with_retry(url)
        if data:
            with open(path, "wb") as f:
                f.write(data)
            return True
        return False
    except Exception as e:
        print(f"Failed download {url}: {e}")
        return False


class FirmwareType(Enum):
    """Enum for different firmware types."""

    EXPRESSLRS = ("ExpressLRS", "firmware", "Firmware")
    BACKPACK = ("Backpack", "backpack", "Backpack")

    def __init__(self, url_path: str, dir_name: str, display_name: str):
        self.url_path = url_path
        self.dir_name = dir_name
        self.display_name = display_name

    def get_local_dir(self, base_path: Path) -> Path:
        """Get local directory path for this firmware type."""
        return base_path / "public" / "assets" / self.dir_name

    def get_index_path(self, base_path: Path) -> Path:
        """Get local index.json path for this firmware type."""
        return self.get_local_dir(base_path) / "index.json"

    def get_index_url(self) -> str:
        """Get remote index.json URL for this firmware type."""
        return f"https://artifactory.expresslrs.org/{self.url_path}/index.json"


def get_artifacts(firmware_type: FirmwareType) -> tuple[bool, list[tuple[str, str]]]:
    """Download and extract firmware artifacts for a specific firmware type.

    Args:
        firmware_type: Type of firmware to download (EXPRESSLRS or BACKPACK)

    Returns:
        True if updates were downloaded, False if already up-to-date or failed
    """
    assets = WEB_SOURCE_DIR / "public" / "assets"
    dest_dir = assets / firmware_type.dir_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Download and parse index
    index_url = (
        f"https://artifactory.expresslrs.org/{firmware_type.url_path}/index.json"
    )
    index_path = dest_dir / "index.json"

    print(f"Downloading {firmware_type.display_name} index...")
    if not fetch_url_to(index_path, index_url):
        print(f"Failed to download {firmware_type.display_name} index")
        return (False, [])

    # Parse index and check if all tags exist with content
    with open(index_path, "r", encoding="utf-8") as f:
        idx = json.load(f)

    tag_items: list[tuple[str, str]] = []
    # Collect tags and branches from index (tag_name -> commit_hash)
    for k in ("tags", "branches"):
        v = idx.get(k)
        if isinstance(v, dict):  # tags and branches are dictionaries
            tag_items.extend(v.items())  # Get (tag_name, commit_hash) pairs

    # Check if all commit hash folders exist with content
    print(f"Checking {firmware_type.display_name} local content...")

    # Filter and download only valid version tags
    seen = set()
    for tag_name, commit_hash in sorted(tag_items, key=lambda x: x[0], reverse=True):
        if tag_name in seen:
            continue
        seen.add(tag_name)

        # Only process tags matching X.Y.Z format
        if not is_valid_version_tag(tag_name):
            continue

        hash_dest = dest_dir / commit_hash

        # Check if commit hash folder exists and has content
        if hash_dest.exists():
            # Check if folder has any files (not just empty)
            has_content = any(hash_dest.iterdir())
            if has_content:
                print(
                    f"{firmware_type.display_name} {tag_name} ({commit_hash[:8]}) already exists with content, skipping"
                )
                continue
            else:
                # Folder exists but empty - remove it and re-download
                print(
                    f"{firmware_type.display_name} {tag_name} ({commit_hash[:8]}) folder exists but empty, removing and re-downloading"
                )
                shutil.rmtree(hash_dest)
        else:
            print(
                f"{firmware_type.display_name} {tag_name} ({commit_hash[:8]}) not found locally, will download"
            )

        print(
            f"Downloading {firmware_type.display_name.lower()} for {tag_name} ({commit_hash[:8]})"
        )
        zip_url = f"https://artifactory.expresslrs.org/{firmware_type.url_path}/{commit_hash}/firmware.zip"

        with tempfile.TemporaryDirectory() as td:
            tmpzip = Path(td) / "firmware.zip"
            if not fetch_url_to(tmpzip, zip_url):
                continue

            hash_dest.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(tmpzip, "r") as z:
                    z.extractall(hash_dest)
                # Some zips contain a top-level firmware/ dir -- move contents up
                sub = hash_dest / "firmware"
                if sub.exists() and sub.is_dir():
                    for item in sub.iterdir():
                        shutil.move(str(item), str(hash_dest))
                    shutil.rmtree(sub)
            except zipfile.BadZipFile:
                print(f"Bad zip from {zip_url}")

    # Download hardware targets only for ExpressLRS firmware
    if firmware_type == FirmwareType.EXPRESSLRS:
        hw_url = "https://artifactory.expresslrs.org/ExpressLRS/hardware.zip"
        hw_dir = dest_dir / "hardware"
        print("Downloading hardware targets...")

        with tempfile.TemporaryDirectory() as td:
            tmpzip = Path(td) / "hardware.zip"
            if fetch_url_to(tmpzip, hw_url):
                # Remove old hardware and replace with new
                if hw_dir.exists() or hw_dir.is_symlink():
                    if hw_dir.is_symlink():
                        hw_dir.unlink()
                    elif hw_dir.is_dir():
                        shutil.rmtree(hw_dir)
                    else:
                        hw_dir.unlink()
                hw_dir.mkdir(parents=True, exist_ok=True)
                try:
                    with zipfile.ZipFile(tmpzip, "r") as z:
                        z.extractall(hw_dir)
                except zipfile.BadZipFile:
                    print("Bad hardware.zip")

    return (True, tag_items)


def refresh_web_source():
    print("Refreshing web source...")
    code, out = run(["git", "pull"], cwd=str(WEB_SOURCE_DIR), capture=True)
    print(out)
    if "Already up to date." in out or "Already up-to-date." in out:
        print("Web source already up to date")
        return False
    return code == 0 and ("Already" not in out)


def refresh_target_source():
    print("Refreshing target source...")
    if not TARGETS_DIR.exists():
        print("Cloning ExpressLRSTargets...")
        code, _ = run(
            ["git", "clone", "https://github.com/z-line/targets.git", str(TARGETS_DIR)]
        )
        if code != 0:
            print("Clone failed")
            return False
    code, out = run(["git", "pull"], cwd=str(TARGETS_DIR), capture=True)
    print(out)
    if "Already up to date." in out or "Already up-to-date." in out:
        print("Target source already up to date")
        return False
    return code == 0 and ("Already" not in out)


def is_valid_version_tag(tag: str) -> bool:
    """Check if tag matches strict 'X.Y.Z' or 'vX.Y.Z' format."""
    pattern = r"^v?\d+\.\d+\.\d+$"
    return bool(re.match(pattern, tag))


def firmware_overlay(tags_map: list[tuple[str, str]]):
    # Convert list of tuples to dictionary for lookup
    tags_dict = dict(tags_map)

    repo_url = "https://github.com/ExpressLRS/ExpressLRS.git"
    repo_dir = WEB_SOURCE_DIR / "ExpressLRS"
    if not repo_dir.exists():
        print("Cloning ExpressLRS...")
        code, _ = run(["git", "clone", repo_url, str(repo_dir)])
        if code != 0:
            print("Failed to clone ExpressLRS")
            return False

    # update
    code, out = run(["git", "pull"], cwd=str(repo_dir), capture=True)
    print(out)

    # list tags
    code, tag_out = run(
        ["git", "tag", "-l", "--sort=v:refname"], cwd=str(repo_dir), capture=True
    )
    tags = [t.strip() for t in tag_out.splitlines() if t.strip()]
    to_build = []
    for tag in tags:
        if not is_valid_version_tag(tag):
            continue

        # Parse version and filter >= 3.6.0
        version_str = tag.lstrip("v")
        parts = version_str.split(".")
        try:
            major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
            # Only include versions >= 3.6.0
            if (major, minor, patch) >= (3, 6, 0):
                to_build.append(tag)
        except (ValueError, IndexError):
            continue

    if not to_build:
        print("No valid tags to build >= 3.6.0 (format: X.Y.Z)")
        return False

    pio_envs = ["Unified_ESP32_LR1121_TX_via_ETX"]

    orig_branch_code, orig_branch = run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo_dir), capture=True
    )
    orig_branch = orig_branch.strip()
    
    # build with platformio
    pio_bin = shutil.which("pio") or shutil.which("platformio")
    if not pio_bin:
        print("PlatformIO not found; skipping builds")

    # Install/update PlatformIO packages
    print("Installing PlatformIO packages...")
    run([pio_bin, "pkg", "install", "--platform", "native"], cwd=str(repo_dir / "src"))
    run([pio_bin, "pkg", "update"], cwd=str(repo_dir / "src"))

    for tag in to_build:
        print(f"Building tag {tag}")

        # Clean working directory before checkout
        run(["git", "reset", "--hard"], cwd=str(repo_dir))
        run(["git", "clean", "-fd"], cwd=str(repo_dir))
        run(["git", "checkout", orig_branch], cwd=str(repo_dir))

        # Checkout tag directly (not as branch)
        code, _ = run(["git", "checkout", tag], cwd=str(repo_dir))
        if code != 0:
            print(f"Failed to checkout tag {tag}, skipping")
            continue

        # apply patches
        patches_dir = WEB_SOURCE_DIR / "patches"
        if patches_dir.exists():
            for p in sorted(patches_dir.glob("*.patch")):
                print(f"Applying patch {p.name}")
                code, _ = run(["git", "apply", str(p)], cwd=str(repo_dir), capture=True)
                if code != 0:
                    # try git am
                    code, _ = run(
                        ["git", "am", "--signoff", str(p)],
                        cwd=str(repo_dir),
                        capture=True,
                    )

        for env in pio_envs:
            print(f"Building for env {env}")
            
            # Remove _via_* suffix to get clean target name
            target_name = env.split("_via_")[0]
            
            # Determine build configurations based on target type
            builds = []
            if "2400" in env or env.startswith("FM30"):
                # 2400MHz targets
                builds = [
                    ("LBT", "-DRegulatory_Domain_EU_CE_2400"),
                    ("FCC", "-DRegulatory_Domain_ISM_2400"),
                ]
            elif "LR1121" in env:
                # LR1121 targets
                builds = [
                    ("LBT", "-DRegulatory_Domain_EU_CE_2400 -DRegulatory_Domain_FCC_915"),
                    ("FCC", "-DRegulatory_Domain_FCC_915"),
                ]
            else:
                # Other targets (915MHz, etc.)
                builds = [
                    ("FCC", "-DRegulatory_Domain_FCC_915"),
                ]

            # Run builds with different regulatory domains
            for region, build_flags in builds:
                print(f"  Building {env} for {region} region...")
                
                # Set build flags via environment variable
                env_vars = os.environ.copy()
                env_vars["PLATFORMIO_BUILD_FLAGS"] = build_flags
                
                # Run PlatformIO build
                result = subprocess.run(
                    [pio_bin, "run", "-e", env],
                    cwd=str(repo_dir / "src"),
                    env=env_vars,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True
                )
                
                if result.returncode != 0:
                    print(f"    Build failed for {env} ({region})")
                    continue
                
                # Define source and destination directories
                build_dir = repo_dir / "src" / ".pio" / "build" / env
                dest_dir = (
                    FirmwareType.EXPRESSLRS.get_local_dir(WEB_SOURCE_DIR)
                    / tags_dict[tag]
                    / region
                    / target_name
                )
                dest_dir.mkdir(parents=True, exist_ok=True)
                
                # Move artifacts (.elrs and .bin files)
                copied = False
                for ext in ("*.elrs", "*.bin"):
                    for f in build_dir.glob(ext):
                        shutil.move(str(f), str(dest_dir / f.name))
                        copied = True
                
                if copied:
                    print(f"    Moved artifacts to {dest_dir}")

    # Return to original branch and clean up
    run(["git", "reset", "--hard"], cwd=str(repo_dir))
    run(["git", "clean", "-fd"], cwd=str(repo_dir))
    run(["git", "checkout", orig_branch], cwd=str(repo_dir))

    return True


def soft_link_targets():
    firmware_assets = FirmwareType.EXPRESSLRS.get_local_dir(WEB_SOURCE_DIR)
    if not firmware_assets.exists():
        return
    for d in firmware_assets.iterdir():
        if not d.is_dir():
            continue
        if d.name == "hardware":
            continue
        hw_link = d / "hardware"
        if hw_link.exists() or hw_link.is_symlink():
            if hw_link.is_dir() and not hw_link.is_symlink():
                shutil.rmtree(hw_link)
            else:
                try:
                    hw_link.unlink()
                except Exception:
                    pass
        try:
            os.symlink(str(TARGETS_DIR), str(hw_link))
            print(f"Linked hardware for {d.name}")
        except Exception as e:
            print(f"Could not link for {d}: {e}")

    # top-level hardware
    top_hw = firmware_assets / "hardware"
    if top_hw.exists():
        if top_hw.is_symlink() or top_hw.is_file():
            top_hw.unlink()
        else:
            shutil.rmtree(top_hw)
    try:
        os.symlink(str(TARGETS_DIR), str(top_hw))
    except Exception as e:
        print(f"Could not create top-level hardware symlink: {e}")


def deploy():
    deploy_dir = WEB_SOURCE_DIR / "deploy_config"
    dist_dir = WEB_SOURCE_DIR / "dist"
    if not deploy_dir.exists() or not dist_dir.exists():
        print("Missing deploy_config or dist, skipping deploy")
        return
    for cfg in deploy_dir.glob("*.json"):
        name = cfg.stem
        target = WEB_SOURCE_DIR.parent / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        # copy dist
        for item in dist_dir.iterdir():
            dest = target / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
        shutil.copy2(cfg, target / "config.json")
        print(f"Deployed to {target}")
        # create symlinks for firmware hardware
        firmware_dir = target / "assets" / FirmwareType.EXPRESSLRS.dir_name
        if firmware_dir.exists():
            for d in firmware_dir.iterdir():
                if not d.is_dir() or d.name == "hardware":
                    continue
                hw = d / "hardware"
                if hw.exists() or hw.is_symlink():
                    if hw.is_dir() and not hw.is_symlink():
                        shutil.rmtree(hw)
                    else:
                        hw.unlink()
                os.symlink(str(TARGETS_DIR), str(hw))
            top_hw = firmware_dir / "hardware"
            if top_hw.exists():
                if top_hw.is_symlink() or top_hw.is_file():
                    top_hw.unlink()
                else:
                    shutil.rmtree(top_hw)
            os.symlink(str(TARGETS_DIR), str(top_hw))


def rebuild_web():
    print("Rebuilding web assets...")
    run(["npm", "install"], cwd=str(WEB_SOURCE_DIR))
    run(["npm", "run", "build"], cwd=str(WEB_SOURCE_DIR))


def main():
    firmware_update = False
    expressLRS_tag = []
    # Check and download firmware for all types
    for firmware_type in FirmwareType:
        print(f"Checking {firmware_type.display_name} artifacts...")
        ret, tags = get_artifacts(firmware_type)
        if ret:
            firmware_update = True
            if firmware_type == FirmwareType.EXPRESSLRS:
                expressLRS_tag = tags
    if not firmware_update:
        print("No firmware updates found.")
    soft_link_targets()
    source_update = False
    firmware_changed = firmware_overlay(expressLRS_tag)
    web_changed = refresh_web_source()
    targets_changed = refresh_target_source()
    if web_changed or targets_changed:
        source_update = True

    if firmware_update or source_update or firmware_changed:
        rebuild_web()
        deploy()
    else:
        print("No changes detected. Skipping web rebuild.")


if __name__ == "__main__":
    main()
