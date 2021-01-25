import contextlib
import functools
import itertools
import operator
import os
import subprocess
import tempfile
from pathlib import Path

import typer
import yaml

from html2dash import custom_builder

DOCSET_EXT = ".tar.xz"

BASE_URL = "https://github.com"
TMPDIR = tempfile.gettempdir()
REPODIR = Path(TMPDIR) / 'repos'
REPODIR.mkdir(parents=True, exist_ok=True)

HOME_DIR = Path(".").absolute()
ICON_DIR = HOME_DIR / "icons"
DOCSET_DIR = HOME_DIR / "docsets"
DOCSET_DIR.mkdir(parents=True, exist_ok=True)

FEED_DIR = HOME_DIR / "feeds"
FEED_DIR.mkdir(parents=True, exist_ok=True)


def validate_generator(generator: str):
    generators = ["doc2dash", "html2dash"]
    if generator not in generators:
        message = f"`{generator}` generator is not supported. Valid values are: {generators}."
        raise ValueError(message)


@contextlib.contextmanager
def working_directory(path):
    """Changes working directory and returns to previous on exit."""
    prev_cwd = Path.cwd()
    os.chdir(path)
    try:
        yield
        _ = Path.cwd()
    finally:
        os.chdir(prev_cwd)


def _build_project(
    name,
    repo,
    doc_dir='docs',
    html_pages_dir='_build/html',
    doc_build_cmd="make -j4 html",
    generator="doc2dash",
    install=True,
):

    try:
        local_dir = REPODIR / name
        doc_dir = local_dir / doc_dir
        validate_generator(generator)
        kwargs = dict(shell=True, check=True)

        if not local_dir.exists():
            repo_link = f"{BASE_URL}/{repo}"
            command = [
                "git",
                "clone",
                "--recurse-submodules",
                repo_link,
                local_dir,
            ]
            subprocess.run(command, check=True)
        else:
            print(f"{name} directory already exits.")

        with working_directory(local_dir):

            if install:
                command = ["python", "-m", "pip", "install", ".", "--no-deps"]
                subprocess.run(command, check=True)
            latest_tag = os.popen("git rev-parse --short HEAD").read().strip()
            if not latest_tag:
                latest_tag = "unknown"

        with working_directory(doc_dir):
            command = doc_build_cmd
            subprocess.run(command, **kwargs)

        icon_dir = ICON_DIR / name
        icons = []
        icon_files = None
        if icon_dir.exists():
            icon_files = list(icon_dir.iterdir())

        source = doc_dir / html_pages_dir
        if generator == "doc2dash":
            command = [
                "doc2dash",
                "--force",
                "--index-page",
                "index.html",
                "--enable-js",
                "--name",
                name,
                source.as_posix(),
                "--destination",
                DOCSET_DIR.as_posix(),
            ]
            if icon_files:
                icons = [
                    ["--icon", icon.as_posix()] for icon in icon_files if icon.suffix == '.png'
                ]
                icons = list(itertools.chain(*icons))
                command += icons
            command = " ".join(command)
            subprocess.run(command, **kwargs)

        elif generator == "html2dash":
            if icon_files:
                icon = icon_files[0]
            else:
                icon = None

            custom_builder(
                name=name,
                destination=DOCSET_DIR.as_posix(),
                index_page="index.html",
                source=source.as_posix(),
                icon=icon,
            )

        else:
            raise RuntimeError(f"Unknown generator: {generator}")

        with working_directory(DOCSET_DIR):
            tar_command = [
                "tar",
                "--exclude='.DS_Store'",
                "-Jcvf",
                f"{name}{DOCSET_EXT}",
                f"{name}.docset",
            ]

            # Compress the result docset with maximum compression with xz:
            my_env = os.environ.copy()
            my_env["XZ_OPT"] = "-9"
            subprocess.run(tar_command, check=True, env=my_env)
            subprocess.run(f"rm -rf {name}.docset", **kwargs)
        create_feed(name, latest_tag)
    except Exception as exc:
        print(exc)


def create_feed(name, latest_tag):
    from xml.etree.ElementTree import Element, SubElement, tostring

    from bs4 import BeautifulSoup

    feed_filename = f"{FEED_DIR}/{name}.xml"
    base_url = "https://raw.githubusercontent.com/andersy005/dash-docsets/docsets/docsets"

    entry = Element("entry")
    version = SubElement(entry, "version")
    version.text = f"main@{latest_tag}"
    url = SubElement(entry, "url")
    url.text = f"{base_url}/{name}{DOCSET_EXT}"

    bs = BeautifulSoup(tostring(entry), features="html.parser").prettify()

    with open(feed_filename, "w") as f:
        f.write(bs)


app = typer.Typer(help='Dash docset builder')


@app.command()
def build(
    name,
    repo,
    doc_dir: str = typer.Option(
        'docs', help='Directory containing source files for documentation.'
    ),
    html_pages_dir: str = typer.Option(
        '_build/html', help='location of built html pages relative to `doc_dir`'
    ),
    doc_build_cmd: str = typer.Option(
        "make -j4 html", help='custom command to use when building the docs. Defaults to None'
    ),
    generator: str = typer.Option("doc2dash", help="Documentation Set generator."),
    install: bool = typer.Option(True, help="Whether to install the package in editable mode"),
):
    """Build dash docset for given project/repo"""

    _build_project(name, repo, doc_dir, html_pages_dir, doc_build_cmd, generator, install)


@app.command()
def build_from_config(
    config: Path = typer.Argument(
        None, exists=True, file_okay=True, help='YAML config file to use'
    ),
    key: str = typer.Option(
        None, "--key", "-k", help='Key corresponding to list of project to build docsets for.'
    ),
):
    """
    Build docsets for a set of projects defined in a config file.
    """
    with open(config) as fpt:
        data = yaml.safe_load(fpt)

    if key:
        data = data[key]

    else:
        data = functools.reduce(operator.iconcat, data.values(), [])
    for project in data:
        project_info = project.copy()

        name = project_info.pop('name')
        repo = project_info.pop('repo')

        _build_project(name, repo, **project_info)


@app.command()
def update_feed_list(
    feed_file: Path = typer.Argument(f"{FEED_DIR}/README.md"),
    root: str = typer.Option(
        "https://raw.githubusercontent.com/andersy005/dash-docsets/docsets/feeds"
    ),
):
    """Update docsets feed list"""

    items = list(Path(DOCSET_DIR).rglob(f"*{DOCSET_EXT}"))
    if items:
        print(f"Found {len(items)} items.")
        print(items)
        items.sort()
        with open(feed_file, "w") as fpt:
            print(
                "# Docset Feeds\n\nYou can subscribe to the following feeds with a single click.\n\n```bash\n dash-feed://<URL encoded feed URL>\n```\n",
                file=fpt,
            )
            for item in items:
                print(f"- **{item.stem}**: {root}/{item.stem}.xml", file=fpt)

            print(
                "\n![](https://github.com/andersy005/dash-docsets/raw/master/images/how-to-add-feed.png)",
                file=fpt,
            )
    else:
        print("Didn't find any files....")


if __name__ == "__main__":
    typer.run(app())
