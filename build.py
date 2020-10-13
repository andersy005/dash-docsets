import concurrent.futures
import contextlib
import itertools
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import click
import yaml

ERRORS = []
FNULL = open(os.devnull, "w")

HOME_DIR = Path(".").absolute()
DOCSET_DIR = HOME_DIR / "docsets"
ICON_DIR = HOME_DIR / "icons"
DOCSET_DIR.mkdir(parents=True, exist_ok=True)
FEED_DIR = HOME_DIR / "feeds"
FEED_DIR.mkdir(parents=True, exist_ok=True)


def build_feed_readme(
    feed_dir=FEED_DIR,
    root="https://raw.githubusercontent.com/andersy005/dash-docsets/docsets/feeds",
    feed_file=FEED_DIR / "README.md",
):
    p = Path(feed_dir)
    files = sorted(p.rglob("*.xml"))
    with open(feed_file, "w") as fpt:
        print(
            "# Docset Feeds\n\nYou can subscribe to the following feeds with a single click.\n\n```bash\n dash-feed://<URL encoded feed URL>\n```\n",
            file=fpt,
        )
        for file in files:
            print(f"- **{file.stem}**: {root}/{file.stem}.xml", file=fpt)

        print("\n![](../images/how-to-add-feed.png)", file=fpt)


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


def create_feed(project_info, latest_tag):
    from xml.etree.ElementTree import Element, SubElement, tostring

    from bs4 import BeautifulSoup

    feed_filename = f"{FEED_DIR}/{project_info['name']}.xml"
    base_url = "https://raw.githubusercontent.com/andersy005/dash-docsets/docsets/docsets"

    entry = Element("entry")
    version = SubElement(entry, "version")
    version.text = f"master@{latest_tag}"
    url = SubElement(entry, "url")
    url.text = f"{base_url}/{project_info['name']}.tgz"

    bs = BeautifulSoup(tostring(entry), features="html.parser").prettify()

    with open(feed_filename, "w") as f:
        f.write(bs)


def build_docset(project_info, local_store):
    """
    Build Dash Docset for a project
    """

    latest_tag = ""

    try:
        base_url = "https://github.com"
        repo_link = f"{base_url}/{project_info['repo']}.git"
        folder_name = local_store / project_info["name"]
        doc_dir = folder_name / project_info["doc_dir"]
        cmd = [
            "git",
            "clone",
            "--depth",
            "1",
            "--recurse-submodules",
            repo_link,
            folder_name.as_posix(),
        ]
        subprocess.check_call(cmd)

        with working_directory(folder_name):
            latest_tag = os.popen("git rev-parse --short HEAD").read().strip()
            if not latest_tag:
                latest_tag = "unknown"

        with working_directory(doc_dir):
            if "script" in project_info:
                cmd = project_info["script"]
                out = subprocess.check_call(cmd, shell=True, stdout=FNULL, stderr=sys.stderr)
            else:
                cmd = ["make", "html"]
                out = subprocess.check_call(cmd, stdout=FNULL, stderr=sys.stderr)

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

                subprocess.check_call(cmd, stdout=FNULL, stderr=sys.stderr)

                cmd = [
                    "mv",
                    f'{project_info["name"]}.docset',
                    f'{DOCSET_DIR.as_posix()}/{project_info["name"]}.docset',
                ]
                subprocess.check_call(cmd, stdout=FNULL, stderr=sys.stderr)

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

            subprocess.check_call(cmd, stdout=FNULL, stderr=sys.stderr)

        with working_directory(DOCSET_DIR):

            tar_cmd = [
                "tar",
                "--exclude='.DS_Store'",
                "-cvzf",
                f"{project_info['name']}.tgz",
                f"{project_info['name']}.docset",
            ]

            subprocess.check_call(tar_cmd, stdout=FNULL, stderr=sys.stderr)
        create_feed(project_info, latest_tag)
    except Exception as exc:
        raise RuntimeError(f"project_info={project_info}") from exc


@click.command()
@click.option(
    "-c",
    "--config",
    type=click.Path(exists=True),
    show_default=True,
)
@click.option("-k", "--key", type=click.STRING, show_default=True)
def _main(config, key):

    with open(HOME_DIR / config) as f:
        data = yaml.safe_load(f)

    with tempfile.TemporaryDirectory() as local_store:
        local_store = Path(local_store)
        projects = data["docsets"][key]

        max_workers = len(projects)

        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
            future_tasks = [
                executor.submit(build_docset, project, local_store) for project in projects
            ]
            for future in concurrent.futures.as_completed(future_tasks):
                try:
                    _ = future.result()
                except Exception as exc:
                    ERRORS.append(exc)

        with working_directory(DOCSET_DIR):
            subprocess.check_call(["ls"], stdout=FNULL, stderr=sys.stderr)
            cmd = "rm -rf *.docset"
            subprocess.check_call(cmd, shell=True, stdout=FNULL, stderr=sys.stderr)
            subprocess.check_call(["ls"])

    print("*" * 60)
    if ERRORS:
        print(f"{len(ERRORS)} ERRORS and/or EXCEPTIONS")
        for error in ERRORS:
            print(error)
    else:
        print("NO ERRORS")
    print("*" * 60)

    build_feed_readme()


if __name__ == "__main__":
    try:
        _main()
    except Exception as exc:
        print(f"Encountered an exception: {exc}")
