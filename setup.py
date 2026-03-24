from setuptools import setup, find_packages


def get_version():
    with open("src/remotely/_script.py", "r") as f:
        for line in f:
            if line.startswith("VERSION"):
                return line.split("=")[1].strip().strip("\"'")
    raise RuntimeError("VERSION not found")


setup(
    name="remotely-ssh",
    version=get_version(),
    description="Fuzzy file search for local and remote filesystems",
    packages=find_packages("src"),
    package_dir={"": "src"},
    extras_require={
        "dev": [
            "pre-commit<3.0.0",
            "pytest<7",
        ],
    },
    entry_points={
        "console_scripts": [
            "remotely=remotely:main",
            "remotely-preview=remotely:main",
            "remotely-open=remotely:main",
            "remotely-remote-reload=remotely:main",
            "remotely-remote-preview=remotely:main",
            "remotely-copy=remotely:main",
        ]
    },
)
