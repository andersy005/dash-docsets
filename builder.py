import contextlib
import enum
import itertools
import os
import pathlib
import platform
import re
import shlex
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import field
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

import pandas as pd
import psutil
import pydantic
import ruamel.yaml
import typer
from bs4 import BeautifulSoup
from rich.console import Console
from rich.progress import track

from html2dash import custom_builder

app = typer.Typer(help='Dash docset builder')
console = Console()
error_console = Console(stderr=True, style='bold red')

SYSTEM = platform.system().lower()
MAKE_CMD = 'make html' if SYSTEM == 'darwin' else f'make -j{psutil.cpu_count()} html'
DOCSET_EXT = '.tar.gz'

BASE_URL = 'https://github.com'
DEFAULT_DOCSET_BASE_URL = (
    'https://github.com/andersy005/dash-docsets/releases/download/docsets-latest'
)
DEFAULT_FEED_ROOT_URL = (
    'https://github.com/andersy005/dash-docsets/releases/download/docsets-latest'
)
TMPDIR = tempfile.gettempdir()
REPODIR = pathlib.Path(TMPDIR) / 'repos'
REPODIR.mkdir(parents=True, exist_ok=True)

HOME_DIR = pathlib.Path('.').absolute()
ICON_DIR = HOME_DIR / 'icons'
DOCSET_DIR = HOME_DIR / 'docsets'
DOCSET_DIR.mkdir(parents=True, exist_ok=True)

FEED_DIR = HOME_DIR / 'feeds'
FEED_DIR.mkdir(parents=True, exist_ok=True)

PROJECT_PIXI_DIR = '.dash-docsets-pixi'
DEFAULT_PROJECT_PIXI_PYTHON = '3.13.*'
DEFAULT_PROJECT_PIXI_CHANNELS = ['conda-forge']
DEFAULT_PROJECT_PIXI_PLATFORMS = ['linux-64', 'osx-arm64']


def _env_or_default(name: str, default: str) -> str:
    value = os.getenv(name)
    if not value:
        return default
    return value.rstrip('/')


DOCSET_BASE_URL = _env_or_default('DOCSET_BASE_URL', DEFAULT_DOCSET_BASE_URL)
FEED_ROOT_URL = _env_or_default('FEED_ROOT_URL', DEFAULT_FEED_ROOT_URL)


def _stream_command(
    cmd: str | Sequence[str | os.PathLike[str]],
    no_newline_regexp: str = 'Progress',
    **kwargs: Any,
) -> Iterator[str]:
    """Stream command output while suppressing matching noisy progress lines."""

    if isinstance(cmd, str):
        command = shlex.split(cmd)
    else:
        command = [os.fspath(part) for part in cmd]

    console.log(command)

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **kwargs,
    )
    for line in iter(process.stdout.readline, ''):
        if not re.search(no_newline_regexp, line):
            yield line
    process.stdout.close()
    if return_code := process.wait():
        error_console.print(process.stderr.read())
        raise subprocess.CalledProcessError(return_code, command)


def stream_command(
    cmd: str | Sequence[str | os.PathLike[str]],
    no_newline_regexp: str = 'Progress',
    **kwargs: Any,
) -> None:
    for _ in _stream_command(cmd, no_newline_regexp, **kwargs):
        pass


@contextlib.contextmanager
def working_directory(path: str | os.PathLike[str]) -> Iterator[None]:
    """Changes working directory and returns to previous on exit."""
    prev_cwd = pathlib.Path.cwd()

    os.chdir(path)
    console.log(f'Current directory: {path}')
    try:
        yield
        _ = pathlib.Path.cwd()
    finally:
        os.chdir(prev_cwd)


class Generator(enum.StrEnum):
    doc2dash = 'doc2dash'
    html2dash = 'html2dash'


class Project(pydantic.BaseModel):
    name: str
    repo: str
    generator: Generator = 'doc2dash'
    doc_dir: str = 'docs'
    doc_build_cmd: str = MAKE_CMD
    html_pages_dir: str = '_build/html'
    install: bool = True
    use_pixi_env: bool = True
    pixi_python: str = DEFAULT_PROJECT_PIXI_PYTHON
    pixi_channels: list[str] = pydantic.Field(
        default_factory=lambda: list(DEFAULT_PROJECT_PIXI_CHANNELS)
    )
    pixi_platforms: list[str] = pydantic.Field(
        default_factory=lambda: list(DEFAULT_PROJECT_PIXI_PLATFORMS)
    )
    pixi_dependencies: dict[str, str] = pydantic.Field(default_factory=dict)


@pydantic.dataclasses.dataclass
class Builder:
    projects: list[Project]
    docset_base_url: str = DOCSET_BASE_URL
    errors: list[str] = field(default_factory=list)

    def _project_manifest_path(self, project: Project, local_dir: pathlib.Path) -> pathlib.Path:
        project_pixi_dir = local_dir / PROJECT_PIXI_DIR
        project_pixi_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = project_pixi_dir / 'pyproject.toml'

        workspace_name = re.sub(r'[^a-z0-9-]+', '-', project.name.lower()).strip('-')
        if not workspace_name:
            workspace_name = 'dash-docset'

        dependency_map = {'python': project.pixi_python, 'pip': '*'}
        dependency_map.update(project.pixi_dependencies)

        if not project.pixi_channels:
            raise ValueError(f'Project {project.name!r} must define at least one pixi channel')
        if not project.pixi_platforms:
            raise ValueError(f'Project {project.name!r} must define at least one pixi platform')

        channels = ', '.join(f'"{channel}"' for channel in project.pixi_channels)
        platforms = ', '.join(f'"{platform}"' for platform in project.pixi_platforms)
        dependency_lines = '\n'.join(
            f'    "{name}" = "{spec}"' for name, spec in sorted(dependency_map.items())
        )

        manifest_text = (
            '[tool.pixi.workspace]\n'
            f'    name = "{workspace_name}"\n'
            f'    channels = [{channels}]\n'
            f'    platforms = [{platforms}]\n\n'
            '[tool.pixi.dependencies]\n'
            f'{dependency_lines}\n'
        )
        manifest_path.write_text(manifest_text, encoding='utf-8')
        return manifest_path

    def _prepare_project_environment(
        self, project: Project, local_dir: pathlib.Path
    ) -> pathlib.Path:
        manifest_path = self._project_manifest_path(project, local_dir)
        stream_command(['pixi', 'install', '--manifest-path', manifest_path.as_posix()])
        return manifest_path

    def _run_in_project_environment(
        self, manifest_path: pathlib.Path, command: list[str], cwd: pathlib.Path
    ) -> None:
        pixi_command = ['pixi', 'run', '--manifest-path', manifest_path.as_posix(), *command]
        stream_command(pixi_command, cwd=cwd)

    def _latest_commit(self, local_dir: pathlib.Path) -> str:
        try:
            latest_tag = subprocess.check_output(
                ['git', 'rev-parse', '--short', 'HEAD'], text=True, cwd=local_dir
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            latest_tag = 'unknown'
        return latest_tag or 'unknown'

    def _build_docs(self, project: Project) -> tuple[str, str]:
        local_dir = REPODIR / project.name
        doc_dir = local_dir / project.doc_dir

        if not local_dir.exists():
            repo_link = f'{BASE_URL}/{project.repo}'
            command = [
                'git',
                'clone',
                '--recurse-submodules',
                repo_link,
                local_dir,
            ]
            stream_command(command)
        else:
            console.log(f'{project.name} directory already exits.')

        if not doc_dir.exists():
            raise FileNotFoundError(f'Documentation directory does not exist: {doc_dir}')

        latest_tag = self._latest_commit(local_dir)
        if project.use_pixi_env:
            manifest_path = self._prepare_project_environment(project, local_dir)
            if project.install:
                self._run_in_project_environment(
                    manifest_path, ['python', '-m', 'pip', 'install', '-e', '.'], cwd=local_dir
                )
            self._run_in_project_environment(
                manifest_path,
                ['/bin/bash', '-lc', project.doc_build_cmd],
                cwd=doc_dir,
            )
        else:
            with working_directory(local_dir):
                if project.install:
                    stream_command(['python', '-m', 'pip', 'install', '-e', '.'])
                with working_directory(project.doc_dir):
                    stream_command(project.doc_build_cmd)

        icon_dir = ICON_DIR / project.name
        icon_files = sorted(icon_dir.iterdir()) if icon_dir.exists() else None
        source = doc_dir / project.html_pages_dir
        if not source.exists():
            raise FileNotFoundError(f'Built HTML directory does not exist: {source}')
        if project.generator == 'doc2dash':
            command = [
                'doc2dash',
                '--force',
                '--index-page',
                'index.html',
                '--enable-js',
                '--name',
                project.name,
                source.as_posix(),
                '--destination',
                DOCSET_DIR.as_posix(),
            ]
            if icon_files:
                icons = [
                    ['--icon', icon.as_posix()]
                    for icon in icon_files
                    if icon.suffix.lower() == '.png'
                ]
                icons = list(itertools.chain(*icons))
                command += icons
            stream_command(command)

        elif project.generator == 'html2dash':
            icon = icon_files[0] if icon_files else None
            custom_builder(
                name=project.name,
                destination=DOCSET_DIR.as_posix(),
                index_page='index.html',
                source=source.as_posix(),
                icon=icon,
            )

        with working_directory(DOCSET_DIR):
            docset_path = f'{project.name}.docset'
            dir_to_delete = docset_path

            tar_command = [
                'tar',
                "--exclude='.DS_Store'",
                '-czvf',
                f'{project.name}{DOCSET_EXT}',
                docset_path,
            ]
            stream_command(tar_command)
            stream_command(['rm', '-rf', dir_to_delete])

        return project.name, latest_tag

    def _create_feed(self, name: str, latest_tag: str) -> None:
        feed_filename = f'{FEED_DIR}/{name}.xml'
        docset_base_url = self.docset_base_url.rstrip('/')

        entry = Element('entry')
        pkg_name = SubElement(entry, 'name')
        pkg_name.text = f'{name}'
        version = SubElement(entry, 'version')
        version.text = f'main@{latest_tag}'
        url = SubElement(entry, 'url')
        url.text = f'{docset_base_url}/{name}{DOCSET_EXT}'

        bs = BeautifulSoup(tostring(entry), features='html.parser').prettify()

        with open(feed_filename, 'w', encoding='utf-8') as f:
            f.write(bs)

    def create_docset(self, project: Project) -> None:
        name, latest_tag = self._build_docs(project)
        self._create_feed(name, latest_tag)

    def build_all(self) -> None:
        self.errors = []
        for project in track(self.projects):
            try:
                self.create_docset(project)
            except Exception:
                self.errors.append(project.name)

        if self.errors:
            error_console.print('Errors occured while building docsets:')
            error_console.print(self.errors)


@app.command()
def build(
    config: pathlib.Path = typer.Argument(
        ..., exists=True, file_okay=True, help='YAML config file to use'
    ),
    docset_base_url: str = typer.Option(
        DOCSET_BASE_URL,
        help='Base URL where docset archives are hosted.',
    ),
):
    """Build docset"""

    yaml_loader = ruamel.yaml.YAML(typ='safe', pure=True)
    with open(config, encoding='utf-8') as f:
        config_data = yaml_loader.load(f)

    if not isinstance(config_data, list):
        raise TypeError(
            f'Expected a list of project mappings in {config}, got {type(config_data)!r}'
        )

    projects = [Project(**p) for p in config_data]
    builder = Builder(projects=projects, docset_base_url=docset_base_url)
    builder.build_all()


@app.command()
def update_feed_list(
    feed_file: pathlib.Path = typer.Argument(f'{FEED_DIR}/README.md'),
    docset_dir: pathlib.Path = typer.Option(DOCSET_DIR, help='docset directory'),
    feed_root_url: str = typer.Option(
        FEED_ROOT_URL,
        help='Root URL for the feeds',
    ),
):
    """Update docsets feed list"""

    if items := list(pathlib.Path(docset_dir).rglob(f'*{DOCSET_EXT}')):
        console.log(f'✅ Found {len(items)} items.')
        items.sort()
        console.log(items)
        with open(feed_file, 'w') as fpt:
            fpt.write(
                '# Docset Feeds\n\nYou can subscribe to the following feeds with a single click.\n\n'
                '```bash\n'
                ' dash-feed://<URL encoded feed URL>\n'
                '```\n'
            )
            fpt.write(
                '\n![dash-docsets](https://github.com/andersy005/dash-docsets/raw/main/images/how-to-add-feed.png)\n'
            )
            entries = []
            for item in track(items):
                entry = item.name.split('.')[0]
                entries.append(
                    {
                        'Name': entry,
                        'Feed URL': f'{feed_root_url}/{entry}.xml',
                        'Size': f'{item.stat().st_size / (1024 * 1024):.1f} MB',
                    }
                )

            table = pd.DataFrame(entries).to_markdown(tablefmt='github')
            fpt.write(f'{table}\n')

    else:
        console.log("❌ Didn't find any files...", style='red')


if __name__ == '__main__':
    typer.run(app())
