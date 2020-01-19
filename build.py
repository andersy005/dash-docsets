import contextlib
import itertools
import os
import subprocess
import tempfile
from pathlib import Path
from pprint import pprint as print

import yaml

HOME_DIR = Path(".").absolute()
DOCSET_DIR = HOME_DIR / "docsets"
ICON_DIR = HOME_DIR / "icons"
DOCSET_DIR.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def working_directory(path):
    """Changes working directory and returns to previous on exit."""
    prev_cwd = Path.cwd()
    os.chdir(path)
    try:
        yield
        doc_dir = Path.cwd()
        print(f"Documentation Directory: {doc_dir}")
    finally:
        os.chdir(prev_cwd)


def build_docset(project_info, local_store):
    """
    Build Dash Docset for a project
    """
    repo_link = project_info["repo"]
    folder_name = local_store / project_info["name"]
    doc_dir = folder_name / project_info["doc_dir"]
    cmd = ["git", "clone", repo_link, folder_name.as_posix()]
    subprocess.check_call(cmd)

    with working_directory(doc_dir):
        cmd = ["make", "html"]
        subprocess.check_call(cmd)

    source = (doc_dir / project_info["html_pages"]).as_posix()
    icon_dir = ICON_DIR / project_info["name"]
    icons = []
    if icon_dir.exists():
        icons = list(icon_dir.iterdir())
        icons = [["--icon", icon.as_posix()] for icon in icons]
        icons = list(itertools.chain(*icons))

    cmd = [
        "doc2dash",
        "--force",
        "--index-page",
        "index.html",
        "--enable-js",
        "--name",
        project_info["name"],
        source,
        "--destination",
        DOCSET_DIR.as_posix(),
        "--quiet",
    ]
    if icons:
        cmd += icons

    subprocess.check_call(cmd)


def _main():

    with open(HOME_DIR / "docsets-config.yaml") as f:
        data = yaml.safe_load(f)

    with tempfile.TemporaryDirectory() as local_store:

        local_store = Path(local_store)
        projects = data["docsets"]
        for project in projects:
            build_docset(project, local_store)


if __name__ == "__main__":
    _main()
