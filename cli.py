import contextlib
import functools
import itertools
import operator
import os
import platform
import re
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

import pandas as pd
import psutil
import typer
import yaml
from bs4 import BeautifulSoup
from rich.console import Console
from rich.progress import track
from rich.table import Table

from html2dash import custom_builder, dash_webgen

SYSTEM = platform.system().lower()
MAKE_CMD = "make html" if SYSTEM == 'darwin' else f"make -j{psutil.cpu_count()} html"

console = Console()
DOCSET_EXT = ".tar.gz"

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


def _stream_command(cmd, no_newline_regexp="Progess", **kwargs):
    """stream a command (yield) back to the user, as each line is available.
    # Example usage:
    results = []
    for line in stream_command(cmd):
        print(line, end="")
        results.append(line)
    Parameters
    ==========
    cmd: the command to send, should be a list for subprocess
    no_newline_regexp: the regular expression to determine skipping a
                       newline. Defaults to finding Progress
    """

    if isinstance(cmd, str):
        cmd = cmd.split(" ")

    console.log(cmd)

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, **kwargs
    )
    for line in iter(process.stdout.readline, ""):
        if not re.search(no_newline_regexp, line):
            yield line
    process.stdout.close()
    return_code = process.wait()
    if return_code:
        print(process.stderr.read(), file=sys.stderr)
        raise subprocess.CalledProcessError(return_code, cmd)


def stream_command(cmd, no_newline_regexp="Progess", **kwargs):
    for _ in _stream_command(cmd, no_newline_regexp, **kwargs):
        pass


def validate_generator(generator: str):
    generators = ["doc2dash", "html2dash", "dash-webgen"]
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
    doc_build_cmd=MAKE_CMD,
    generator="doc2dash",
    install=True,
    url=None,
):

    local_dir = REPODIR / name
    doc_dir = local_dir / doc_dir
    validate_generator(generator)
    kwargs = {}

    if not local_dir.exists():
        repo_link = f"{BASE_URL}/{repo}"
        command = [
            "git",
            "clone",
            "--recurse-submodules",
            repo_link,
            local_dir,
        ]
        stream_command(command)
    else:
        console.log(f"{name} directory already exits.")

    with working_directory(local_dir):

        if install and generator != "dash-webgen":
            command = ["python", "-m", "pip", "install", ".", "--no-deps"]
            stream_command(command)
        latest_tag = os.popen("git rev-parse --short HEAD").read().strip()
        if not latest_tag:
            latest_tag = "unknown"
    if generator != "dash-webgen":
        with working_directory(doc_dir):
            command = doc_build_cmd
            stream_command(command, **kwargs)

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
            icons = [["--icon", icon.as_posix()] for icon in icon_files if icon.suffix == '.png']
            icons = list(itertools.chain(*icons))
            command += icons
        command = " ".join(command)
        stream_command(command, **kwargs)

    elif generator == "html2dash":
        icon = icon_files[0] if icon_files else None
        custom_builder(
            name=name,
            destination=DOCSET_DIR.as_posix(),
            index_page="index.html",
            source=source.as_posix(),
            icon=icon,
        )

    elif generator == "dash-webgen":
        dash_webgen(name=name, url=url, destination=DOCSET_DIR.as_posix())

    else:
        raise RuntimeError(f"Unknown generator: {generator}")

    with working_directory(DOCSET_DIR):
        if generator == "dash-webgen":
            docset_path = f"{name}/{name}.docset"
            dir_to_delete = f"{name}"
        else:
            docset_path = f"{name}.docset"
            dir_to_delete = docset_path

        tar_command = [
            "tar",
            "--exclude='.DS_Store'",
            "-Jcvf",
            f"{name}{DOCSET_EXT}",
            docset_path,
        ]
        stream_command(tar_command)
        stream_command(f"rm -rf {dir_to_delete}", **kwargs)
    create_feed(name, latest_tag)


def create_feed(name, latest_tag):
    feed_filename = f"{FEED_DIR}/{name}.xml"
    base_url = "https://raw.githubusercontent.com/andersy005/dash-docsets/docsets/docsets"

    entry = Element("entry")
    pkg_name = SubElement(entry, "name")
    pkg_name.text = f"{name}"
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
        "make -j10 html", help='custom command to use when building the docs. Defaults to None'
    ),
    generator: str = typer.Option("doc2dash", help="Documentation Set generator."),
    install: bool = typer.Option(True, help="Whether to install the package in editable mode"),
    url: str = typer.Option(None, help="URL of the docset"),
):
    """Build dash docset for given project/repo"""

    _build_project(name, repo, doc_dir, html_pages_dir, doc_build_cmd, generator, install, url)


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
    errors = []
    for project in track(data):
        project_info = project.copy()

        name = project_info.pop('name')
        repo = project_info.pop('repo')

        try:
            _build_project(name, repo, **project_info)
        except Exception:
            errors.append((name, traceback.format_exc()))
    if errors:
        console.rule("Errors")
        table = Table(title="")
        table.add_column("Package/Project", justify="right", style="cyan")
        table.add_column("Traceback", style="magenta")

        for error in errors:
            table.add_row(error[0], error[1])
        console.print(table)


@app.command()
def update_feed_list(
    feed_file: Path = typer.Argument(f"{FEED_DIR}/README.md"),
    docset_dir: Path = typer.Option(DOCSET_DIR, help='docset directory'),
    feed_root_url: str = typer.Option(
        "https://raw.githubusercontent.com/andersy005/dash-docsets/docsets/feeds",
        help="Root URL for the feeds",
    ),
):
    """Update docsets feed list"""

    items = list(Path(docset_dir).rglob(f"*{DOCSET_EXT}"))
    if items:
        console.log(f"✅ Found {len(items)} items.")
        items.sort()
        console.log(items)
        with open(feed_file, "w") as fpt:
            print(
                "# Docset Feeds\n\nYou can subscribe to the following feeds with a single click.\n\n```bash\n dash-feed://<URL encoded feed URL>\n```\n",
                file=fpt,
            )
            print(
                "\n![dash-docsets](https://github.com/andersy005/dash-docsets/raw/main/images/how-to-add-feed.png)",
                file=fpt,
            )
            entries = []
            for item in track(items):
                entry = item.name.split('.')[0]
                entries.append(
                    {
                        'Name': entry,
                        'Feed URL': f'{feed_root_url}/{entry}.xml',
                        'Size': f'{item.stat().st_size / (1024*1024):.1f} MB',
                    }
                )

            table = pd.DataFrame(entries).to_markdown(tablefmt="github")
            print(table, file=fpt)

    else:
        console.log("❌ Didn't find any files...", style='red')


if __name__ == "__main__":
    typer.run(app())
