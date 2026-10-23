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
            "pypihub = pypihub.pypihub:usage",
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
        # What the Flask server needs to run (SQLite works out of the box).
        "flask": ["Flask", "Flask-Limiter>=3.5", "SQLAlchemy>=2.0", "bcrypt", "packaging>=20.9"],
        # Database drivers: pure Python or self-contained wheels, nothing to compile.
        "postgres": ["psycopg[binary]>=3.1"],
        "mysql": ["PyMySQL>=1.1", "cryptography"],
        # Shared cache and rate-limit counters across workers and replicas.
        "redis": ["redis>=5"],
        # README rendering on package pages, the same library pypi.org uses. Without it descriptions show as plain text.
        "readme": ["readme_renderer[md]>=43"],
        "bcrypt": ["bcrypt"],
        "all": ["Flask", "Flask-Limiter>=3.5", "SQLAlchemy>=2.0", "bcrypt", "packaging>=20.9",
                "psycopg[binary]>=3.1", "PyMySQL>=1.1", "cryptography", "redis>=5", "readme_renderer[md]>=43"],
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
    python_requires='>=3.10',
    license="LGPL-3.0-or-later",
)
