"""Prove the shipped artefact works from where a user stands.

A repo can be completely uninstallable while every other check passes, because
contributors run it from the checkout where imports resolve for reasons a user
will not have. This builds the wheel, installs it into a clean virtualenv,
leaves the source tree, and runs the real entry point — including a failure path,
because a packaging fault often first surfaces as an unhandled ImportError that
reads like an application bug.

Marked ``live`` only because building a wheel is slow; it needs no network.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.slow


def _run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=600, **kwargs)


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    if shutil.which("python3") is None:
        pytest.skip("python3 not on PATH")
    work = tmp_path_factory.mktemp("pkg")
    dist = work / "dist"

    build = _run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(dist), str(ROOT)])
    if build.returncode != 0:
        pytest.skip(f"wheel build unavailable here: {build.stderr[-400:]}")
    wheels = list(dist.glob("chotot_cli-*.whl"))
    assert wheels, f"no wheel produced: {build.stdout[-400:]}"

    env_dir = work / "venv"
    venv.create(env_dir, with_pip=True)
    python = env_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
    install = _run([str(python), "-m", "pip", "install", "--quiet", str(wheels[0])])
    assert install.returncode == 0, install.stderr[-600:]
    return {"python": str(python), "wheel": wheels[0],
            "bin": env_dir / ("Scripts" if os.name == "nt" else "bin"), "cwd": str(work)}


def test_wheel_contains_the_bundled_taxonomy(installed):
    """Without these the installed CLI cannot resolve a province or a category."""
    import zipfile

    names = zipfile.ZipFile(installed["wheel"]).namelist()
    for required in ("chotot/data/regions.json", "chotot/data/categories.json",
                     "chotot/data/facets.json"):
        assert required in names, f"{required} is missing from the wheel"


def test_wheel_does_not_ship_internal_tooling(installed):
    """A wheel that quietly exports tests or tools is a surface nobody chose."""
    import zipfile

    names = zipfile.ZipFile(installed["wheel"]).namelist()
    leaked = [n for n in names if n.startswith(("tests/", "tools/"))]
    assert not leaked, f"internal packages leaked into the wheel: {leaked[:5]}"


def test_console_script_runs_from_outside_the_source_tree(installed):
    """cwd is the temp dir, so nothing resolves via the checkout."""
    binary = installed["bin"] / ("chotot.exe" if os.name == "nt" else "chotot")
    assert binary.exists(), "the 'chotot' console script was not installed"
    result = _run([str(binary), "--version"], cwd=installed["cwd"])
    assert result.returncode == 0, result.stderr
    assert "chotot" in result.stdout


def test_offline_commands_work_from_the_installed_copy(installed):
    binary = installed["bin"] / ("chotot.exe" if os.name == "nt" else "chotot")
    result = _run([str(binary), "regions", "--search", "da nang", "--json"],
                  cwd=installed["cwd"])
    assert result.returncode == 0, result.stderr
    entries = json.loads(result.stdout)
    assert entries and any(e["region_v2"] in (3016, 3017) for e in entries)


def test_a_failure_path_is_a_clean_error_not_a_traceback(installed):
    """Packaging faults surface here first, as an ImportError that reads like a bug."""
    binary = installed["bin"] / ("chotot.exe" if os.name == "nt" else "chotot")
    result = _run([str(binary), "search", "x", "--region", "atlantis"], cwd=installed["cwd"])
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "error:" in result.stderr


def test_the_package_imports_without_the_source_tree(installed):
    result = _run([installed["python"], "-c",
                   "import chotot, chotot.taxonomy, chotot.facets, chotot.cli;"
                   "print(chotot.__version__, len(chotot.taxonomy.provinces()))"],
                  cwd=installed["cwd"])
    assert result.returncode == 0, result.stderr
    version, provinces = result.stdout.split()
    assert version == "2.0.0" and int(provinces) > 60
