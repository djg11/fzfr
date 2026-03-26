"""Legacy setup.py for editable installs under older pip/setuptools.

pyproject.toml is the authoritative build configuration. This file exists
only for compatibility with toolchains that do not support PEP 517/518
(e.g. pip install -e . on Python 3.6 without build-isolation support).

Do not add new configuration here -- use pyproject.toml instead.
"""

from __future__ import annotations

import re
from pathlib import Path

from setuptools import find_packages, setup


ROOT = Path(__file__).resolve().parent
SCRIPT_FILE = ROOT / "src" / "remotely" / "_script.py"
README_FILE = ROOT / "README.md"


def get_version() -> str:
    """Read VERSION from _script.py without importing the package."""
    text = SCRIPT_FILE.read_text(encoding="utf-8")

    patterns = (
        r"^\s*VERSION\s*=\s*[\"']([^\"']+)[\"']\s*(?:#.*)?$",
        r"^\s*__version__\s*=\s*[\"']([^\"']+)[\"']\s*(?:#.*)?$",
    )

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.MULTILINE)
        if match:
            return match.group(1)

    raise RuntimeError(
        f"VERSION not found in {SCRIPT_FILE}. Expected a line like: VERSION = '0.1.0'"
    )


long_description = README_FILE.read_text(encoding="utf-8")

setup(
    name="remotely-ssh",
    version=get_version(),
    description="Zero-install SSH file transport for fuzzy-finders (fzf, Television, etc.)",
    long_description=long_description,
    long_description_content_type="text/markdown",
    license="MIT",
    packages=find_packages("src"),
    package_dir={"": "src"},
    python_requires=">=3.6",
    install_requires=[
        "dataclasses; python_version < '3.7'",
    ],
    extras_require={
        # Dev extras require Python 3.10+ to install.
        # Use 'make test36' for runtime verification under Python 3.6.
        "dev": [
            "ruff>=0.15.0",
            "pre-commit>=3.0.0",
            "pyright>=1.1.0",
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "remotely=remotely:main",
            "remotely-preview=remotely:main",
            "remotely-remote-reload=remotely:main",
            "remotely-remote-preview=remotely:main",
        ]
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
        "Operating System :: MacOS",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Utilities",
    ],
    project_urls={
        "Homepage": "https://github.com/djg11/remotely",
        "Repository": "https://github.com/djg11/remotely",
        "Issues": "https://github.com/djg11/remotely/issues",
        "Changelog": "https://github.com/djg11/remotely/releases",
    },
)
