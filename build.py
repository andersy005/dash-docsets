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
FEED_DIR = HOME_DIR / "feeds"
FEED_DIR.mkdir(parents=True, exist_ok=True)


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


def create_feed(project_info):
    from xml.etree.ElementTree import Element, SubElement, tostring
    from bs4 import BeautifulSoup
    from github import Github

    g = Github()

    repo = g.get_repo(project_info["repo"])
    latest_tag = list(repo.get_tags())[0].name

    feed_filename = f"{FEED_DIR}/{project_info['name']}.xml"
    base_url = (
        "https://raw.githubusercontent.com/andersy005/dash-docsets/docsets/docsets"
    )

    entry = Element("entry")
    version = SubElement(entry, "version")
    version.text = f"master.post.{latest_tag}"
    url = SubElement(entry, "url")
    url.text = f"{base_url}/{project_info['name']}.tgz"

    bs = BeautifulSoup(tostring(entry), features="html.parser").prettify()

    with open(feed_filename, "w") as f:
        f.write(bs)


def build_docset(project_info, local_store):
    """
    Build Dash Docset for a project
    """
    base_url = "https://github.com"
    repo_link = f"{base_url}/{project_info['repo']}.git"
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
    ]
    if icons:
        cmd += icons

    subprocess.check_call(cmd)

    with working_directory(DOCSET_DIR):

        tar_cmd = [
            "tar",
            "--exclude='.DS_Store'",
            "-cvzf",
            f"{project_info['name']}.tgz",
            f"{project_info['name']}.docset",
        ]

        subprocess.check_call(tar_cmd)

        cmd = ["rm", "-rf", f"{project_info['name']}.docset"]

        subprocess.check_call(cmd)

    create_feed(project_info)


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
