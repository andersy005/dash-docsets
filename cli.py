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
from click.core import Argument, Command

TMPDIR = tempfile.gettempdir()
REPODIR = Path(TMPDIR) / 'repos'
REPODIR.mkdir(parents=True, exist_ok=True)

HOME_DIR = Path(".").absolute()
ICON_DIR = HOME_DIR / "icons"
DOCSET_DIR = HOME_DIR / "docsets"
DOCSET_DIR.mkdir(parents=True, exist_ok=True)


def validate_generator(generator: str):
    generators = ["doc2dash"]
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
):

    try:
        local_dir = REPODIR / name
        doc_dir = local_dir / doc_dir
        validate_generator(generator)
        kwargs = dict(shell=True, check=True)

        if not local_dir.exists():
            command = [
                "gh",
                "repo",
                "clone",
                repo,
                "--",
                local_dir,
                "--depth",
                "1",
                "--recurse-submodules",
            ]
            subprocess.run(command, check=True)
        else:
            print(f"{name} directory already exits.")

        with working_directory(local_dir):
            latest_tag = os.popen("git rev-parse --short HEAD").read().strip()
            if not latest_tag:
                latest_tag = "unknown"

        with working_directory(doc_dir):
            command = doc_build_cmd
            subprocess.run(command, **kwargs)

        icon_dir = ICON_DIR / name
        icons = []
        if icon_dir.exists():
            icons = list(icon_dir.iterdir())
            icons = [["--icon", icon.as_posix()] for icon in icons if icon.suffix == '.png']
            icons = list(itertools.chain(*icons))

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
            if icons:
                command += icons
        command = " ".join(command)
        subprocess.run(command, **kwargs)

        with working_directory(DOCSET_DIR):
            tar_command = [
                "tar",
                "--exclude='.DS_Store'",
                "-cvzf",
                f"{name}.tgz",
                f"{name}.docset",
            ]

            subprocess.run(tar_command, check=True)
            subprocess.run(f"rm -rf {name}.docset", **kwargs)
    except Exception as exc:
        print(exc)


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
):
    """Build dash docset for given project/repo"""

    _build_project(name, repo, doc_dir, html_pages_dir, doc_build_cmd, generator)


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


if __name__ == "__main__":
    typer.run(app())
