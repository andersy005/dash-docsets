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
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

import pydantic
import ruamel.yaml
import typer
from bs4 import BeautifulSoup
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from html2dash import custom_builder

app = typer.Typer(help='Dash docset builder')
console = Console()
error_console = Console(stderr=True, style='bold red')

SYSTEM = platform.system().lower()
MAKE_CMD = 'make html' if SYSTEM == 'darwin' else f'make -j{os.cpu_count() or 1} html'
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
DEFAULT_PIXI_INSTALL_TIMEOUT_SECONDS = 900
DEFAULT_INSTALL_TIMEOUT_SECONDS = 900
DEFAULT_DOC_BUILD_TIMEOUT_SECONDS = 1200


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
    timeout_seconds: float | None = None,
    shell: bool = False,
    **kwargs: Any,
) -> Iterator[str]:
    """Stream command output while suppressing matching noisy progress lines.

    When *shell* is True the command is passed as a string to the shell,
    which enables compound commands (``&&``, pipes, etc.).  When False the
    command is split with :func:`shlex.split` and executed directly.
    """

    if shell:
        command: str | list[str] = cmd if isinstance(cmd, str) else ' '.join(str(p) for p in cmd)
    elif isinstance(cmd, str):
        command = shlex.split(cmd)
    else:
        command = [os.fspath(part) for part in cmd]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=shell,
        **kwargs,
    )
    recent_stdout: deque[str] = deque(maxlen=500)
    timed_out = False
    timer: threading.Timer | None = None

    def _kill_process() -> None:
        nonlocal timed_out
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            process.kill()

    if timeout_seconds is not None and timeout_seconds > 0:
        timer = threading.Timer(timeout_seconds, _kill_process)
        timer.daemon = True
        timer.start()

    stderr_output = ''
    try:
        for line in iter(process.stdout.readline, ''):
            if not re.search(no_newline_regexp, line):
                recent_stdout.append(line)
                yield line
        process.stdout.close()
        stderr_output = process.stderr.read()
        process.stderr.close()
        return_code = process.wait()
    finally:
        if timer is not None:
            timer.cancel()

    if timed_out and timeout_seconds is not None:
        raise subprocess.TimeoutExpired(
            command,
            timeout_seconds,
            output=''.join(recent_stdout),
            stderr=stderr_output,
        )

    if return_code:
        if stderr_output:
            error_console.print(stderr_output)
        raise subprocess.CalledProcessError(
            return_code,
            command,
            output=''.join(recent_stdout),
            stderr=stderr_output,
        )


def stream_command(
    cmd: str | Sequence[str | os.PathLike[str]],
    no_newline_regexp: str = 'Progress',
    timeout_seconds: float | None = None,
    shell: bool = False,
    **kwargs: Any,
) -> None:
    for _ in _stream_command(
        cmd,
        no_newline_regexp,
        timeout_seconds=timeout_seconds,
        shell=shell,
        **kwargs,
    ):
        pass


@contextlib.contextmanager
def working_directory(path: str | os.PathLike[str]) -> Iterator[None]:
    """Changes working directory and restores the previous one on exit."""
    prev_cwd = pathlib.Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev_cwd)


class Generator(enum.StrEnum):
    doc2dash = 'doc2dash'
    html2dash = 'html2dash'


class BuildStatus(enum.StrEnum):
    success = 'success'
    failure = 'failure'


class Project(pydantic.BaseModel):
    name: str
    repo: str
    generator: Generator = 'doc2dash'
    doc_dir: str = 'docs'
    doc_build_cmd: str = MAKE_CMD
    html_pages_dir: str = '_build/html'
    install: bool = True
    # When True, skip the custom pixi manifest and run doc_build_cmd directly
    # via the shell in doc_dir.  Use this for projects that ship their own
    # pixi/uv environment (set doc_dir to "." so commands run from repo root).
    use_own_env: bool = False
    # Editable-install command used by the custom-pixi path.  Override when
    # the project requires extras or a requirements file, e.g.
    #   "pip install -e .[docs]"
    #   "pip install -r docs/requirements.txt && pip install -e ."
    pip_install_cmd: str = 'pip install -e .'
    pixi_python: str = DEFAULT_PROJECT_PIXI_PYTHON
    pixi_channels: list[str] = pydantic.Field(
        default_factory=lambda: list(DEFAULT_PROJECT_PIXI_CHANNELS)
    )
    pixi_platforms: list[str] = pydantic.Field(
        default_factory=lambda: list(DEFAULT_PROJECT_PIXI_PLATFORMS)
    )
    pixi_dependencies: dict[str, str] = pydantic.Field(default_factory=dict)
    pixi_pypi_dependencies: dict[str, str] = pydantic.Field(default_factory=dict)
    pixi_install_timeout_seconds: int = DEFAULT_PIXI_INSTALL_TIMEOUT_SECONDS
    install_timeout_seconds: int = DEFAULT_INSTALL_TIMEOUT_SECONDS
    doc_build_timeout_seconds: int = DEFAULT_DOC_BUILD_TIMEOUT_SECONDS


@dataclass
class BuildResult:
    name: str
    status: BuildStatus
    elapsed: float
    error: str | None = None


@pydantic.dataclasses.dataclass
class Builder:
    projects: list[Project]
    docset_base_url: str = DOCSET_BASE_URL
    results: list[BuildResult] = field(default_factory=list)

    def _build_project_dependency_map(self, project: Project) -> dict[str, str]:
        """Merge a minimal base set with the project's explicit pixi_dependencies."""
        dependency_map: dict[str, str] = {
            'python': project.pixi_python,
            'pip': '*',
        }
        dependency_map.update(project.pixi_dependencies)
        return dependency_map

    def _write_project_manifest(
        self,
        project: Project,
        local_dir: pathlib.Path,
        dependency_map: dict[str, str],
    ) -> pathlib.Path:
        project_pixi_dir = local_dir / PROJECT_PIXI_DIR
        project_pixi_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = project_pixi_dir / 'pyproject.toml'

        workspace_name = re.sub(r'[^a-z0-9-]+', '-', project.name.lower()).strip('-')
        if not workspace_name:
            workspace_name = 'dash-docset'

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

        if project.pixi_pypi_dependencies:
            pypi_lines = '\n'.join(
                f'    "{name}" = "{spec}"'
                for name, spec in sorted(project.pixi_pypi_dependencies.items())
            )
            manifest_text += f'\n[tool.pixi.pypi-dependencies]\n{pypi_lines}\n'

        manifest_path.write_text(manifest_text, encoding='utf-8')
        return manifest_path

    def _prepare_project_environment(
        self, project: Project, local_dir: pathlib.Path
    ) -> pathlib.Path:
        dependency_map = self._build_project_dependency_map(project)
        manifest_path = self._write_project_manifest(project, local_dir, dependency_map)
        stream_command(
            ['pixi', 'install', '--manifest-path', manifest_path.as_posix()],
            timeout_seconds=project.pixi_install_timeout_seconds,
        )
        return manifest_path

    def _run_in_project_environment(
        self,
        manifest_path: pathlib.Path,
        command: list[str],
        cwd: pathlib.Path,
        timeout_seconds: float | None = None,
    ) -> None:
        pixi_command = ['pixi', 'run', '--manifest-path', manifest_path.as_posix(), *command]
        stream_command(pixi_command, cwd=cwd, timeout_seconds=timeout_seconds)

    def _latest_commit(self, local_dir: pathlib.Path) -> str:
        try:
            latest_tag = subprocess.check_output(
                ['git', 'rev-parse', '--short', 'HEAD'], text=True, cwd=local_dir
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            latest_tag = 'unknown'
        return latest_tag or 'unknown'

    def _build_docs(
        self,
        project: Project,
        on_step: Callable[[str], None] | None = None,
    ) -> tuple[str, str]:
        def step(label: str) -> None:
            if on_step is not None:
                on_step(label)

        local_dir = REPODIR / project.name
        doc_dir = local_dir / project.doc_dir

        step('cloning repo')
        if not local_dir.exists():
            repo_link = f'{BASE_URL}/{project.repo}'
            stream_command(
                ['git', 'clone', '--recurse-submodules', repo_link, local_dir],
            )
        else:
            console.log(f'{project.name} repo already cloned, reusing.')

        if not doc_dir.exists():
            raise FileNotFoundError(f'Documentation directory does not exist: {doc_dir}')

        latest_tag = self._latest_commit(local_dir)

        if project.use_own_env:
            # Delegate entirely to the project's own environment manager
            # (pixi, uv, etc.).  The doc_build_cmd is run as a shell command
            # so compound commands (&&, pipes) work too.
            step('building docs (own env)')
            stream_command(
                project.doc_build_cmd,
                cwd=doc_dir,
                timeout_seconds=project.doc_build_timeout_seconds,
                shell=True,
            )
        else:
            step('installing pixi env')
            manifest_path = self._prepare_project_environment(project, local_dir)

            if project.install:
                step('pip installing project')
                self._run_in_project_environment(
                    manifest_path,
                    ['/bin/bash', '-c', project.pip_install_cmd],
                    cwd=local_dir,
                    timeout_seconds=project.install_timeout_seconds,
                )

            step('building docs')
            self._run_in_project_environment(
                manifest_path,
                ['/bin/bash', '-c', project.doc_build_cmd],
                cwd=doc_dir,
                timeout_seconds=project.doc_build_timeout_seconds,
            )

        step('packaging docset')
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
                command += list(itertools.chain(*icons))
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
            tar_command = [
                'tar',
                "--exclude='.DS_Store'",
                '-czvf',
                f'{project.name}{DOCSET_EXT}',
                docset_path,
            ]
            stream_command(tar_command)
            stream_command(['rm', '-rf', docset_path])

        return project.name, latest_tag

    def _create_feed(self, name: str, latest_tag: str) -> None:
        feed_filename = f'{FEED_DIR}/{name}.xml'
        docset_base_url = self.docset_base_url.rstrip('/')

        entry = Element('entry')
        pkg_name = SubElement(entry, 'name')
        pkg_name.text = name
        version = SubElement(entry, 'version')
        version.text = f'main@{latest_tag}'
        url = SubElement(entry, 'url')
        url.text = f'{docset_base_url}/{name}{DOCSET_EXT}'

        bs = BeautifulSoup(tostring(entry), features='html.parser').prettify()

        with open(feed_filename, 'w', encoding='utf-8') as f:
            f.write(bs)

    def create_docset(
        self,
        project: Project,
        on_step: Callable[[str], None] | None = None,
    ) -> None:
        name, latest_tag = self._build_docs(project, on_step=on_step)
        self._create_feed(name, latest_tag)

    def build_all(self) -> None:
        self.results = []
        for project in self.projects:
            console.rule(f'Building {project.name}', style='cyan')
            start = time.monotonic()
            with Progress(
                SpinnerColumn(),
                TextColumn('[bold blue]{task.fields[project]}[/]'),
                TextColumn('•'),
                TextColumn('[white]{task.description}[/]'),
                TimeElapsedColumn(),
                console=console,
                transient=True,
            ) as progress:
                task_id = progress.add_task('starting', project=project.name, total=None)

                def update(label: str, _task_id=task_id, _progress=progress) -> None:
                    _progress.update(_task_id, description=label)

                try:
                    self.create_docset(project, on_step=update)
                except subprocess.TimeoutExpired as timeout_error:
                    elapsed = time.monotonic() - start
                    message = (
                        f'timed out after {timeout_error.timeout}s while running:'
                        f' {timeout_error.cmd}'
                    )
                    self.results.append(
                        BuildResult(project.name, BuildStatus.failure, elapsed, message)
                    )
                    error_console.print(f'✗ {project.name} — {message}')
                except Exception as error:
                    elapsed = time.monotonic() - start
                    self.results.append(
                        BuildResult(project.name, BuildStatus.failure, elapsed, str(error))
                    )
                    error_console.print(f'✗ {project.name} — {error}')
                else:
                    elapsed = time.monotonic() - start
                    self.results.append(BuildResult(project.name, BuildStatus.success, elapsed))
                    console.print(
                        f'[bold green]✓[/] [bold]{project.name}[/] [dim]({elapsed:.1f}s)[/]'
                    )

        self._print_summary()

    def _print_summary(self) -> None:
        if not self.results:
            return

        table = Table(title='Build summary', show_lines=False)
        table.add_column('Project', style='bold')
        table.add_column('Status')
        table.add_column('Elapsed', justify='right')
        table.add_column('Error', overflow='fold')

        for result in self.results:
            status_text = (
                '[green]✓ success[/]'
                if result.status == BuildStatus.success
                else '[red]✗ failure[/]'
            )
            table.add_row(
                result.name,
                status_text,
                f'{result.elapsed:.1f}s',
                result.error or '',
            )

        console.print(table)

        failed = [result.name for result in self.results if result.status == BuildStatus.failure]
        if failed:
            error_console.print(f'{len(failed)} project(s) failed: {", ".join(failed)}')


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

    items = sorted(pathlib.Path(docset_dir).rglob(f'*{DOCSET_EXT}'))
    if not items:
        console.print('[red]❌ No docsets found[/]')
        return

    console.print(f'[green]✅ Found {len(items)} docset(s)[/]')

    lines = [
        '# Docset Feeds\n',
        '\nYou can subscribe to the following feeds with a single click.\n',
        '\n```bash\n dash-feed://<URL encoded feed URL>\n```\n',
        '\n![dash-docsets](https://github.com/andersy005/dash-docsets/raw/main/images/how-to-add-feed.png)\n\n',
        '| Name | Feed URL | Size |\n',
        '| --- | --- | --- |\n',
    ]
    for item in items:
        entry = item.name.split('.')[0]
        size_mb = item.stat().st_size / (1024 * 1024)
        lines.append(f'| {entry} | {feed_root_url}/{entry}.xml | {size_mb:.1f} MB |\n')

    with open(feed_file, 'w') as fpt:
        fpt.writelines(lines)


if __name__ == '__main__':
    app()
