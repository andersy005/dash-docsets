import contextlib
import enum
import itertools
import os
import pathlib
import platform
import re
import subprocess
import sys
import tempfile
import typing

import psutil
import pydantic
import ruamel.yaml
import typer
from rich.console import Console

from html2dash import custom_builder

app = typer.Typer(help='Dash docset builder')
console = Console()

SYSTEM = platform.system().lower()
MAKE_CMD = "make html" if SYSTEM == 'darwin' else f"make -j{psutil.cpu_count()} html"
DOCSET_EXT = ".tar.gz"

BASE_URL = "https://github.com"
TMPDIR = tempfile.gettempdir()
REPODIR = pathlib.Path(TMPDIR) / 'repos'
REPODIR.mkdir(parents=True, exist_ok=True)

HOME_DIR = pathlib.Path(".").absolute()
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


@contextlib.contextmanager
def working_directory(path):
    """Changes working directory and returns to previous on exit."""
    prev_cwd = pathlib.Path.cwd()

    os.chdir(path)
    console.log(f'Current directory: {path}')
    try:
        yield
        _ = pathlib.Path.cwd()
    finally:
        os.chdir(prev_cwd)


class Generator(str, enum.Enum):
    doc2dash = 'doc2dash'
    html2dash = 'html2dash'


class Project(pydantic.BaseModel):
    name: str
    repo: str
    generator: Generator = 'doc2dash'
    doc_dir: str = 'docs'
    doc_build_cmd: str = MAKE_CMD
    html_pages_dir: str = '_build/html'
    install: bool = False


@pydantic.dataclasses.dataclass
class Builder:
    projects: typing.List[Project]

    def _build_docs(self, project: Project):
        local_dir = REPODIR / project.name
        doc_dir = local_dir / project.doc_dir
        kwargs = {}

        if not local_dir.exists():
            repo_link = f"{BASE_URL}/{project.repo}"
            command = [
                "git",
                "clone",
                "--recurse-submodules",
                repo_link,
                local_dir,
            ]
            stream_command(command)
        else:
            console.log(f"{project.name} directory already exits.")

        with working_directory(local_dir):
            if project.install:
                command = ["python", "-m", "pip", "install", "-e", ".", "--no-deps"]
                stream_command(command)

            latest_tag = os.popen("git rev-parse --short HEAD").read().strip()
            if not latest_tag:
                latest_tag = "unknown"

            with working_directory(project.doc_dir):
                stream_command(project.doc_build_cmd, **kwargs)

        icon_dir = ICON_DIR / project.name
        icons = []
        icon_files = None
        if icon_dir.exists():
            icon_files = list(icon_dir.iterdir())

        source = doc_dir / project.html_pages_dir
        if project.generator == "doc2dash":
            command = [
                "doc2dash",
                "--force",
                "--index-page",
                "index.html",
                "--enable-js",
                "--name",
                project.name,
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
            stream_command(command, **kwargs)

        elif project.generator == "html2dash":
            icon = icon_files[0] if icon_files else None
            custom_builder(
                name=name,
                destination=DOCSET_DIR.as_posix(),
                index_page="index.html",
                source=source.as_posix(),
                icon=icon,
            )


@app.command()
def build(
    config: pathlib.Path = typer.Argument(
        None, exists=True, file_okay=True, help='YAML config file to use'
    )
):
    """Build docset"""

    with open(config, 'r') as f:
        config = ruamel.yaml.safe_load(f)

    projects = [Project(**p) for p in config]

    builder = Builder(projects=projects)
    builder._build_docs(projects[-1])


if __name__ == '__main__':
    typer.run(app())
