"""Legacy setup.py for editable installs under older pip/setuptools.

pyproject.toml is the authoritative build configuration. This file exists
only for compatibility with toolchains that do not support PEP 517/518
(e.g. pip install -e . on Python 3.6 without build-isolation support).

Do not add new configuration here -- use pyproject.toml instead.
"""

from pathlib import Path

from setuptools import find_packages, setup


def get_version():
    """Read VERSION from _script.py without importing the package."""
    with open("src/remotely/_script.py", "r") as f:
        for line in f:
            if line.startswith("VERSION"):
                return line.split("=")[1].strip().strip("\"'")
    raise RuntimeError("VERSION not found in src/remotely/_script.py")


README = Path(__file__).parent / "README.md"
with README.open("r", encoding="utf-8") as f:
    long_description = f.read()

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
)
