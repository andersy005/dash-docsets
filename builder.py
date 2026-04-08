import ast
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
import tomllib
from collections import deque
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
MKDOCS_VERSION_CONSTRAINT = '>=1.6,<2'
SPHINX_VERSION_CONSTRAINT = '>=8,<9'
SETUPTOOLS_VERSION_CONSTRAINT = '<81'
DOCS_GROUP_NAMES = ('docs', 'doc', 'documentation')
CONF_IMPORT_DEPENDENCY_MAP = {
    'dask_sphinx_theme': 'dask-sphinx-theme',
    'jupyter_sphinx': 'jupyter-sphinx',
    'numpydoc': 'numpydoc',
    'sphinx_autosummary_accessors': 'sphinx-autosummary-accessors',
    'sphinx_click': 'sphinx-click',
    'sphinx_copybutton': 'sphinx-copybutton',
    'sphinx_design': 'sphinx-design',
    'sphinx_remove_toctrees': 'sphinx-remove-toctrees',
    'sphinx_tabs': 'sphinx-tabs',
    'sphinxcontrib.mermaid': 'sphinxcontrib-mermaid',
    'yaml': 'pyyaml',
}
MKDOCS_THEME_DEPENDENCY_MAP = {
    'material': 'mkdocs-material',
}
MKDOCS_PLUGIN_DEPENDENCY_MAP = {
    'exclude': 'mkdocs-exclude',
    'llmstxt': 'mkdocs-llmstxt',
    'macros': 'mkdocs-macros-plugin',
    'mkdocstrings': 'mkdocstrings',
    'redirects': 'mkdocs-redirects',
}


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
    recent_stdout: deque[str] = deque(maxlen=500)
    for line in iter(process.stdout.readline, ''):
        if not re.search(no_newline_regexp, line):
            recent_stdout.append(line)
            yield line
    process.stdout.close()
    stderr_output = process.stderr.read()
    process.stderr.close()
    if return_code := process.wait():
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

    def _infer_build_dependencies(self, command: str) -> dict[str, str]:
        command_lower = command.lower()
        dependencies: dict[str, str] = {}
        if 'sphinx-build' in command_lower:
            dependencies['sphinx'] = SPHINX_VERSION_CONSTRAINT
        if re.search(r'(^|\s)mkdocs(\s|$)', command_lower):
            dependencies['mkdocs'] = MKDOCS_VERSION_CONSTRAINT
        if re.search(r'(^|\s)make(\s|$)', command_lower):
            dependencies['make'] = '*'
        return dependencies

    def _clean_requirement_line(self, value: str) -> str | None:
        cleaned = re.sub(r'\s+#.*$', '', value).strip()
        if not cleaned:
            return None
        if cleaned.startswith('#'):
            return None
        if cleaned.startswith(('-r', '--requirement', '-c', '--constraint', '-e', '--editable')):
            return None
        return cleaned

    def _normalize_package_name(self, name: str) -> str:
        return name.strip().lower().replace('_', '-')

    def _parse_requirement(self, requirement: str) -> tuple[str, str, str] | None:
        cleaned = self._clean_requirement_line(requirement)
        if cleaned is None:
            return None

        candidate = cleaned.split(';', 1)[0].strip()
        if not candidate or candidate.startswith('git+'):
            return None

        if ' @ ' in candidate:
            package_name = candidate.split(' @ ', 1)[0].strip()
            if package_name:
                normalized_name = self._normalize_package_name(package_name)
                return normalized_name, '*', candidate
            return None

        match = re.match(r'^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?\s*(.*)$', candidate)
        if not match:
            return None

        package_name, specifier = match.groups()
        if '/' in package_name or ':' in package_name:
            return None

        normalized_name = self._normalize_package_name(package_name)
        normalized_specifier = specifier.strip() or '*'
        return normalized_name, normalized_specifier, candidate

    def _requirement_to_conda_dependency(self, requirement: str) -> tuple[str, str] | None:
        parsed = self._parse_requirement(requirement)
        if parsed is None:
            return None
        name, specifier, _pip_requirement = parsed
        return name, specifier

    def _requirement_to_pip_requirement(self, requirement: str) -> tuple[str, str] | None:
        parsed = self._parse_requirement(requirement)
        if parsed is None:
            return None
        name, _specifier, pip_requirement = parsed
        return name, pip_requirement

    def _docs_requirements_files(
        self, local_dir: pathlib.Path, doc_dir: pathlib.Path
    ) -> list[pathlib.Path]:
        files: list[pathlib.Path] = []
        seen: set[pathlib.Path] = set()

        def _collect(path: pathlib.Path) -> None:
            if path.is_file() and path not in seen:
                seen.add(path)
                files.append(path)

        roots = (doc_dir, local_dir / 'docs')
        for root in roots:
            _collect(root / 'requirements.txt')
            _collect(root / 'requirements-docs.txt')
            requirements_dir = root / 'requirements'
            if requirements_dir.is_dir():
                for item in sorted(requirements_dir.glob('*.txt')):
                    _collect(item)

        _collect(local_dir / 'requirements-docs.txt')
        return files

    def _extract_docs_specs_from_pyproject(
        self, local_dir: pathlib.Path, doc_dir: pathlib.Path
    ) -> list[str]:
        pyproject_candidates = [
            local_dir / 'pyproject.toml',
            doc_dir / 'pyproject.toml',
            doc_dir.parent / 'pyproject.toml',
        ]
        specs: list[str] = []
        seen_paths: set[pathlib.Path] = set()

        for pyproject_file in pyproject_candidates:
            if pyproject_file in seen_paths or not pyproject_file.exists():
                continue
            seen_paths.add(pyproject_file)

            specs.extend(self._extract_docs_specs_from_single_pyproject(pyproject_file))

        return list(dict.fromkeys(specs))

    def _extract_docs_specs_from_single_pyproject(self, pyproject_file: pathlib.Path) -> list[str]:
        try:
            pyproject_data = tomllib.loads(pyproject_file.read_text(encoding='utf-8'))
        except (OSError, tomllib.TOMLDecodeError):
            return []

        specs: list[str] = []
        project_table = pyproject_data.get('project', {})
        optional_dependencies = project_table.get('optional-dependencies', {})

        for group_name in DOCS_GROUP_NAMES:
            group_deps = optional_dependencies.get(group_name, [])
            if isinstance(group_deps, list):
                specs.extend([dep for dep in group_deps if isinstance(dep, str)])

        dependency_groups = pyproject_data.get('dependency-groups', {})

        def _expand_group(group_name: str, visited: set[str]) -> None:
            if group_name in visited:
                return
            visited.add(group_name)
            group_values = dependency_groups.get(group_name, [])
            if not isinstance(group_values, list):
                return
            for entry in group_values:
                if isinstance(entry, str):
                    specs.append(entry)
                elif isinstance(entry, dict):
                    include_group = entry.get('include-group')
                    if isinstance(include_group, str):
                        _expand_group(include_group, visited)

        for group_name in DOCS_GROUP_NAMES:
            _expand_group(group_name, set())

        hatch_docs = pyproject_data.get('tool', {}).get('hatch', {}).get('envs', {}).get('docs', {})
        if isinstance(hatch_docs, dict):
            hatch_extra_deps = hatch_docs.get('extra-dependencies', [])
            if isinstance(hatch_extra_deps, list):
                specs.extend([dep for dep in hatch_extra_deps if isinstance(dep, str)])

            hatch_dependency_groups = hatch_docs.get('dependency-groups', [])
            if isinstance(hatch_dependency_groups, list):
                for group_name in hatch_dependency_groups:
                    if isinstance(group_name, str):
                        _expand_group(group_name, set())

        uv_sources = pyproject_data.get('tool', {}).get('uv', {}).get('sources', {})
        if isinstance(uv_sources, dict):
            return [self._resolve_uv_source(spec, uv_sources) for spec in specs]

        return specs

    def _resolve_uv_source(self, spec: str, uv_sources: dict[str, Any]) -> str:
        parsed = self._parse_requirement(spec)
        if parsed is None:
            return spec

        name, _specifier, _pip_requirement = parsed
        source_value = uv_sources.get(name)
        if not isinstance(source_value, dict):
            return spec

        if isinstance(source_value.get('workspace'), bool) and source_value['workspace']:
            return spec

        git_url = source_value.get('git')
        if isinstance(git_url, str):
            git_reference = ''
            for ref_key in ('rev', 'tag', 'branch'):
                ref_value = source_value.get(ref_key)
                if isinstance(ref_value, str):
                    git_reference = f'@{ref_value}'
                    break

            git_prefix = '' if git_url.startswith('git+') else 'git+'
            subdirectory = source_value.get('subdirectory')
            if isinstance(subdirectory, str):
                return f'{name} @ {git_prefix}{git_url}{git_reference}#subdirectory={subdirectory}'
            return f'{name} @ {git_prefix}{git_url}{git_reference}'

        url = source_value.get('url')
        if isinstance(url, str):
            return f'{name} @ {url}'

        path_value = source_value.get('path')
        if isinstance(path_value, str):
            return f'{name} @ {path_value}'

        return spec

    def _extract_conf_import_dependencies(self, doc_dir: pathlib.Path) -> dict[str, str]:
        conf_candidates = [
            doc_dir / 'conf.py',
            doc_dir / 'source' / 'conf.py',
        ]
        conf_file = next((file for file in conf_candidates if file.exists()), None)
        if conf_file is None:
            return {}

        try:
            tree = ast.parse(conf_file.read_text(encoding='utf-8'))
        except (OSError, SyntaxError):
            return {}

        imported_modules: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == 'extensions':
                        if isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
                            for item in node.value.elts:
                                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                                    imported_modules.add(item.value)
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == 'extensions':
                    value = node.value
                    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                        for item in value.elts:
                            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                                imported_modules.add(item.value)

        dependencies: dict[str, str] = {}
        for module_name in imported_modules:
            for module_prefix, package_name in CONF_IMPORT_DEPENDENCY_MAP.items():
                if module_name == module_prefix or module_name.startswith(f'{module_prefix}.'):
                    dependencies[package_name] = '*'
        return dependencies

    def _extract_mkdocs_dependencies(
        self,
        local_dir: pathlib.Path,
        doc_dir: pathlib.Path,
    ) -> dict[str, str]:
        loader = ruamel.yaml.YAML(typ='safe', pure=True)
        dependencies: dict[str, str] = {}
        candidates = [
            doc_dir / 'mkdocs.yml',
            doc_dir / 'mkdocs.yaml',
            local_dir / 'mkdocs.yml',
            local_dir / 'mkdocs.yaml',
        ]
        seen: set[pathlib.Path] = set()
        for config_file in candidates:
            if config_file in seen or not config_file.exists():
                continue
            seen.add(config_file)
            try:
                config_data = loader.load(config_file.read_text(encoding='utf-8')) or {}
            except (OSError, ruamel.yaml.YAMLError):
                continue
            if not isinstance(config_data, dict):
                continue

            theme = config_data.get('theme')
            theme_name: str | None = None
            if isinstance(theme, dict):
                raw_name = theme.get('name')
                if isinstance(raw_name, str):
                    theme_name = raw_name.strip().lower()
            elif isinstance(theme, str):
                theme_name = theme.strip().lower()
            if theme_name:
                mapped_theme_dependency = MKDOCS_THEME_DEPENDENCY_MAP.get(theme_name)
                if mapped_theme_dependency:
                    dependencies[mapped_theme_dependency] = '*'

            plugins = config_data.get('plugins')
            if isinstance(plugins, list):
                for plugin in plugins:
                    plugin_name: str | None = None
                    if isinstance(plugin, str):
                        plugin_name = plugin
                    elif isinstance(plugin, dict):
                        keys = list(plugin.keys())
                        if keys:
                            first_key = keys[0]
                            if isinstance(first_key, str):
                                plugin_name = first_key
                    if not plugin_name:
                        continue
                    normalized_plugin_name = plugin_name.strip().lower()
                    mapped_plugin_dependency = MKDOCS_PLUGIN_DEPENDENCY_MAP.get(
                        normalized_plugin_name
                    )
                    if mapped_plugin_dependency:
                        dependencies[mapped_plugin_dependency] = '*'

        return dependencies

    def _dependency_to_pip_requirement(self, name: str, specifier: str) -> str:
        if specifier in {'', '*'}:
            return name
        return f'{name}{specifier}'

    def _extract_missing_conda_packages(self, output_text: str) -> list[str]:
        patterns = [
            r'No candidates were found for\s+([A-Za-z0-9_.-]+)',
            r'No candidates found for\s+([A-Za-z0-9_.-]+)',
        ]
        missing: list[str] = []
        for pattern in patterns:
            for match in re.finditer(pattern, output_text, flags=re.DOTALL):
                candidate = self._normalize_package_name(match.group(1))
                if candidate not in missing:
                    missing.append(candidate)
        return missing

    def _discover_docs_dependencies(
        self, project: Project, local_dir: pathlib.Path, doc_dir: pathlib.Path
    ) -> tuple[dict[str, str], dict[str, str]]:
        dependencies: dict[str, str] = {}
        pip_requirements: dict[str, str] = {}

        for requirement_file in self._docs_requirements_files(local_dir, doc_dir):
            try:
                for line in requirement_file.read_text(encoding='utf-8').splitlines():
                    conda_dependency = self._requirement_to_conda_dependency(line)
                    pip_dependency = self._requirement_to_pip_requirement(line)
                    if conda_dependency is not None:
                        name, specifier = conda_dependency
                        dependencies[name] = specifier
                    if pip_dependency is not None:
                        name, pip_requirement = pip_dependency
                        pip_requirements[name] = pip_requirement
            except OSError:
                continue

        for spec in self._extract_docs_specs_from_pyproject(local_dir, doc_dir):
            conda_dependency = self._requirement_to_conda_dependency(spec)
            pip_dependency = self._requirement_to_pip_requirement(spec)
            if conda_dependency is not None:
                name, specifier = conda_dependency
                dependencies[name] = specifier
            if pip_dependency is not None:
                name, pip_requirement = pip_dependency
                pip_requirements[name] = pip_requirement

        conf_dependencies = self._extract_conf_import_dependencies(doc_dir)
        dependencies.update(conf_dependencies)
        for name in conf_dependencies:
            pip_requirements.setdefault(name, name)

        mkdocs_dependencies = self._extract_mkdocs_dependencies(local_dir, doc_dir)
        dependencies.update(mkdocs_dependencies)
        for name in mkdocs_dependencies:
            pip_requirements.setdefault(name, name)

        # `make` and PyPI installs still rely on these base tools.
        dependencies.setdefault('setuptools', SETUPTOOLS_VERSION_CONSTRAINT)
        pip_requirements.setdefault('setuptools', 'setuptools<81')

        if dependencies:
            console.log(f'Discovered docs dependencies for {project.name}: {sorted(dependencies)}')
        return dependencies, pip_requirements

    def _build_project_dependency_map(
        self,
        project: Project,
        discovered_dependencies: dict[str, str] | None = None,
    ) -> dict[str, str]:
        dependency_map = {
            'python': project.pixi_python,
            'pip': '*',
            'setuptools': SETUPTOOLS_VERSION_CONSTRAINT,
        }
        dependency_map.update(self._infer_build_dependencies(project.doc_build_cmd))
        if discovered_dependencies:
            dependency_map.update(discovered_dependencies)
        dependency_map.update(project.pixi_dependencies)
        # Keep pkg_resources available for legacy Sphinx extensions like sphinx-tabs.
        dependency_map['setuptools'] = SETUPTOOLS_VERSION_CONSTRAINT
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
        manifest_path.write_text(manifest_text, encoding='utf-8')
        return manifest_path

    def _project_manifest_path(
        self,
        project: Project,
        local_dir: pathlib.Path,
        discovered_dependencies: dict[str, str] | None = None,
    ) -> pathlib.Path:
        dependency_map = self._build_project_dependency_map(
            project, discovered_dependencies=discovered_dependencies
        )
        return self._write_project_manifest(project, local_dir, dependency_map)

    def _prepare_project_environment(
        self,
        project: Project,
        local_dir: pathlib.Path,
        discovered_dependencies: dict[str, str] | None = None,
        discovered_pip_requirements: dict[str, str] | None = None,
    ) -> tuple[pathlib.Path, list[str]]:
        dependency_map = self._build_project_dependency_map(
            project, discovered_dependencies=discovered_dependencies
        )
        fallback_requirements: dict[str, str] = {}
        fallback_candidates = set(discovered_dependencies or {})

        while True:
            manifest_path = self._write_project_manifest(project, local_dir, dependency_map)
            try:
                stream_command(['pixi', 'install', '--manifest-path', manifest_path.as_posix()])
                return manifest_path, sorted(fallback_requirements.values())
            except subprocess.CalledProcessError as error:
                combined_output = '\n'.join(
                    text for text in (error.output or '', error.stderr or '') if text
                )
                missing_packages = self._extract_missing_conda_packages(combined_output)
                removable = [
                    name
                    for name in missing_packages
                    if name in fallback_candidates and name in dependency_map
                ]
                if not removable:
                    raise

                for name in removable:
                    specifier = dependency_map.pop(name, '*')
                    fallback_requirements[name] = (discovered_pip_requirements or {}).get(
                        name, self._dependency_to_pip_requirement(name, specifier)
                    )

                console.log(f'Falling back to pip for {project.name}: {sorted(removable)}')

    def _run_in_project_environment(
        self,
        manifest_path: pathlib.Path,
        command: list[str],
        cwd: pathlib.Path,
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

        discovered_dependencies, discovered_pip_requirements = self._discover_docs_dependencies(
            project, local_dir, doc_dir
        )
        latest_tag = self._latest_commit(local_dir)
        if project.use_pixi_env:
            manifest_path, pip_requirements = self._prepare_project_environment(
                project,
                local_dir,
                discovered_dependencies=discovered_dependencies,
                discovered_pip_requirements=discovered_pip_requirements,
            )
            if project.install:
                self._run_in_project_environment(
                    manifest_path, ['python', '-m', 'pip', 'install', '-e', '.'], cwd=local_dir
                )
            if pip_requirements:
                self._run_in_project_environment(
                    manifest_path,
                    ['python', '-m', 'pip', 'install', *pip_requirements],
                    cwd=local_dir,
                )
            self._run_in_project_environment(
                manifest_path,
                ['/bin/bash', '-c', project.doc_build_cmd],
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
