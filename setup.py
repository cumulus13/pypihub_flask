from setuptools import setup
from pathlib import Path

long_description = (Path(__file__).parent / "README.md").read_text(encoding="utf-8") if Path("README.md").exists() else ""

def read_version():
    version_file = Path(__file__).parent / "__version__.py"
    if not version_file.is_file():
        return "0.0.1"
    version_ns = {}
    exec(version_file.read_text(), version_ns)
    return version_ns["version"]

setup(
    name="pypihub",
    version=read_version(),
    author="Hadi Cahyadi",
    author_email="cumulus13@gmail.com",
    description="Local PyPI Server with caching and upload support",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/cumulus13/pypihub_flask",
    packages=['pypihub'],
    include_package_data=True,
    entry_points={
        "console_scripts": [
            "pypihub = pypihub:usage",
        ],
    },
    install_requires=[
        "requests",
        "beautifulsoup4",
        "pydebugger",
        "rich",
        "rich-argparse",
        "configset",
    ],
    extras_require={
        "flask": ["Flask"],
        "mysql": ["SQLAlchemy", "mysqlclient"],
        "postgres": ["SQLAlchemy", "psycopg2"],
        "bcrypt": ["bcrypt"],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)",
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
        "Topic :: Software Development :: Libraries",
        "Topic :: Internet :: WWW/HTTP :: Indexing/Search",
    ],
    python_requires='>=3.7',
    license="LGPL-3.0-or-later",
)
