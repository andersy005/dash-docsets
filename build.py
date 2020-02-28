import contextlib
import itertools
import json
import os
import subprocess
import sys
import tempfile
from concurrent import futures
from pathlib import Path
from pprint import pprint as print

import yaml

HOME_DIR = Path(".").absolute()
DOCSET_DIR = HOME_DIR / "docsets"
ICON_DIR = HOME_DIR / "icons"
DOCSET_DIR.mkdir(parents=True, exist_ok=True)
FEED_DIR = HOME_DIR / "feeds"
FEED_DIR.mkdir(parents=True, exist_ok=True)


def dashing_config(name, package, index="index.html", allow_js=True):
    config = {
        "name": name,
        "package": package,
        "index": "index.html",
        "selectors": {"dt a": "Command", "title": "Package"},
        "ignore": [""],
        "icon32x32": "",
        "allowJS": allow_js,
        "ExternalURL": "",
    }

    return config


@contextlib.contextmanager
def working_directory(path):
    """Changes working directory and returns to previous on exit."""
    prev_cwd = Path.cwd()
    os.chdir(path)
    try:
        yield
        doc_dir = Path.cwd()
    finally:
        os.chdir(prev_cwd)


def create_feed(project_info):
    from xml.etree.ElementTree import Element, SubElement, tostring
    from bs4 import BeautifulSoup
    from github import Github

    g = Github()

    repo = g.get_repo(project_info["repo"])
    try:
        latest_tag = list(repo.get_tags())[0].name
    except IndexError:
        latest_tag = "unknown"

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

    try:
        base_url = "https://github.com"
        repo_link = f"{base_url}/{project_info['repo']}.git"
        folder_name = local_store / project_info["name"]
        doc_dir = folder_name / project_info["doc_dir"]
        cmd = ["git", "clone", repo_link, folder_name.as_posix()]
        subprocess.check_call(cmd)

        with working_directory(doc_dir):
            if "script" in project_info:
                cmd = project_info["script"]
                out = subprocess.check_call(
                    cmd, shell=True, stdout=sys.stdout, stderr=sys.stderr
                )
            else:
                cmd = ["make", "html"]
                out = subprocess.check_call(cmd, stdout=sys.stdout, stderr=sys.stderr)

        source = (doc_dir / project_info["html_pages"]).as_posix()
        icon_dir = ICON_DIR / project_info["name"]
        icons = []
        if icon_dir.exists():
            icons = list(icon_dir.iterdir())
            icons = [["--icon", icon.as_posix()] for icon in icons]
            icons = list(itertools.chain(*icons))

        if "use_dashing" in project_info:
            config = dashing_config(project_info["name"], project_info["name"])
            with open(f"{source}/dashing.json", "w") as fp:
                json.dump(config, fp)

            with working_directory(source):
                cmd = [
                    "dashing",
                    "build",
                    "--config",
                    f"{source}/dashing.json",
                ]

                subprocess.check_call(cmd)

                cmd = [
                    "mv",
                    f'{project_info["name"]}.docset',
                    f'{DOCSET_DIR.as_posix()}/{project_info["name"]}.docset',
                ]
                subprocess.check_call(cmd)

        else:
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

        create_feed(project_info)
    except Exception as e:
        print(e)


def _main():

    with open(HOME_DIR / "docsets-config.yaml") as f:
        data = yaml.safe_load(f)

    with tempfile.TemporaryDirectory() as local_store:

        local_store = Path(local_store)
        projects = data["docsets"]

        max_workers = len(projects)

        for project in projects:
            build_docset(project, local_store)

        # with futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        #     future_tasks = [
        #         executor.submit(build_docset, project, local_store)
        #         for project in projects
        #     ]

        with working_directory(DOCSET_DIR):
            subprocess.check_call(["ls"])
            cmd = "rm -rf *.docset"
            subprocess.check_call(cmd, shell=True)
            subprocess.check_call(["ls"])


if __name__ == "__main__":
    _main()
