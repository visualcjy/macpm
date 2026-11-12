from pathlib import Path

from setuptools import find_packages, setup

ROOT = Path(__file__).parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

setup(
    name="macpm",
    version="2.2",
    author="Jinyu Chen",
    author_email="visualcjy@mail.com",
    url="https://github.com/visualcjy/macpm",
    project_urls={
        "Source": "https://github.com/visualcjy/macpm",
        "Issues": "https://github.com/visualcjy/macpm/issues",
        "Changelog": "https://github.com/visualcjy/macpm/blob/main/CHANGELOG.md",
    },
    description="Rootless performance monitoring CLI tool for Apple Silicon",
    long_description=README,
    long_description_content_type="text/markdown",
    license="MIT",
    license_files=("LICENSE",),
    packages=find_packages(exclude=("tests", "tests.*", "packaging")),
    include_package_data=True,
    python_requires=">=3.9",
    install_requires=[
        "dashing>=0.1",
        "psutil>=5.9",
        "humanize>=4.0",
    ],
    entry_points={
        "console_scripts": [
            "macpm = macpm.macpm:main",
            "macpm-dump = macpm.dump:main",
        ]
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Operating System :: MacOS",
        "Environment :: Console",
        "Topic :: System :: Monitoring",
    ],
    keywords="macpm apple-silicon monitor performance ioReport psutil",
    zip_safe=False,
)
