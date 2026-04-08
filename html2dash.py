#! /usr/bin/env python3

from __future__ import annotations

import pathlib
import shutil
import sqlite3
import subprocess

from bs4 import BeautifulSoup
from rich.console import Console

console = Console()


def update_db(name: str, path: str, cur: sqlite3.Cursor) -> None:
    cur.execute('SELECT rowid FROM searchIndex WHERE path = ?', (path,))
    dbpath = cur.fetchone()
    cur.execute('SELECT rowid FROM searchIndex WHERE name = ?', (name,))
    dbname = cur.fetchone()

    if dbpath is None and dbname is None:
        cur.execute(
            'INSERT OR IGNORE INTO searchIndex(name, type, path) VALUES (?, ?, ?)',
            (name, 'Section', path),
        )


def add_urls(docset_path: str | pathlib.Path, cur: sqlite3.Cursor) -> None:
    index_page = pathlib.Path(docset_path) / 'index.html'
    if not index_page.exists():
        raise FileNotFoundError(f'Could not find index page: {index_page}')

    with index_page.open(encoding='utf-8') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')

    for tag in soup.find_all('a', href=True):
        name = tag.get_text(strip=True)
        path = str(tag['href']).strip()
        if not name or not path:
            continue
        if path.split('#', 1)[0] == 'index.html':
            continue
        update_db(name, path, cur)


def add_infoplist(info_path: str | pathlib.Path, index_page: str, docset_name: str) -> None:
    info_path = pathlib.Path(info_path)
    name = pathlib.Path(docset_name).stem
    info = f"""
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>CFBundleIdentifier</key>
        <string>{name}</string>
        <key>CFBundleName</key>
        <string>{name}</string>
        <key>DashDocSetFamily</key>
        <string>{name}</string>
        <key>DocSetPlatformFamily</key>
        <string>{name}</string>
        <key>isDashDocset</key>
        <true/>
        <key>isJavaScriptEnabled</key>
        <true/>
        <key>dashIndexFilePath</key>
        <string>{index_page}</string>
</dict>
</plist>
"""

    try:
        info_path.write_text(info, encoding='utf-8')
        console.log('Create the Info.plist file')
    except Exception as exc:
        console.log('[bold red]Create the Info.plist file failed.[/bold red]')
        clear_trash(docset_name)
        raise exc


def clear_trash(docset_name: str | pathlib.Path) -> None:
    docset_path = pathlib.Path(docset_name)
    try:
        if docset_path.exists():
            shutil.rmtree(docset_path)
            console.log('Cleared generated temporary files')
    except Exception as exc:
        console.log('[bold red]Clearing generated files failed.[/bold red]')
        raise exc


def dash_webgen(name: str, url: str, destination: str | pathlib.Path) -> None:
    """Build dash docset from a URL.
    Parameters
    ----------
    name: str
      Name of the docset
    url: str
      URL of the docset
    destination: str
      Output directory
    """
    destination = pathlib.Path(destination).as_posix()
    command = ['npx', 'dash-webgen', '--name', name, '--url', url, '--out', destination]
    subprocess.run(command, check=True)


def custom_builder(
    name: str | None = None,
    destination: str | pathlib.Path | None = None,
    icon: str | pathlib.Path | None = None,
    index_page: str | None = None,
    source: str | pathlib.Path | None = None,
) -> None:
    """
    Parameters
    ----------

    name: str
        Name the docset explicitly
    destination: str
        Put the resulting docset into PATH
    icon: str
        Add PNG icon FILENAME to docset
    index_page:
        Set the file that is shown
    source:
        Directory containing the HTML documents

    """
    if destination is None:
        raise ValueError('destination is required')
    if source is None:
        raise ValueError('source is required')

    source_dir = pathlib.Path(source)
    if not source_dir.exists() or not source_dir.is_dir():
        raise FileNotFoundError(f'Could not find source directory: {source_dir}')
    dir_name = source_dir.stem

    docset_name = f'{name}.docset' if name else f'{dir_name}.docset'
    docset_name = pathlib.Path(docset_name)

    # create docset directory and copy files
    doc_path = docset_name / 'Contents/Resources/Documents'
    dsidx_path = docset_name / 'Contents/Resources/docSet.dsidx'
    info = docset_name / 'Contents/info.plist'
    icon_path = docset_name / 'icon.png'

    dest_path = pathlib.Path(destination)
    docset_path = dest_path / doc_path
    docset_path.mkdir(parents=True, exist_ok=True)
    console.log(f'Docset folder ready: {docset_path}')

    sqlite_path = dest_path / dsidx_path
    info_path = (dest_path / info).as_posix()
    icon_path = dest_path / icon_path

    docset_path_str = docset_path.as_posix()
    docset_name = docset_name.as_posix()

    # Copy the HTML Documentation to the Docset Folder
    try:
        ignored_names = {'.git', '.doctrees', '_sources'}
        ignored = {'.git/', '.doctrees/', '_sources/', '*/*.ipynb'}
        excludes = [f'--exclude={pat}' for pat in ignored]
        source_items = [
            item.as_posix()
            for item in sorted(source_dir.iterdir())
            if item.name not in ignored_names
        ]
        if not source_items:
            raise RuntimeError(f'No documentation files found in: {source_dir}')
        arg_list = (['rsync', '-avhr', '--progress'] + excludes + source_items) + [docset_path_str]

        subprocess.run(arg_list, check=True)
        console.log('Copied HTML documentation')
    except Exception as exc:
        console.log('[bold red]Copying HTML documentation failed.[/bold red]')
        clear_trash(docset_name)
        raise exc

    # create and connect to SQLite
    try:
        with sqlite3.connect(sqlite_path.as_posix()) as db:
            cur = db.cursor()
            cur.execute('DROP TABLE IF EXISTS searchIndex;')
            cur.execute(
                'CREATE TABLE searchIndex(id INTEGER PRIMARY KEY, name TEXT, type TEXT, path TEXT);'
            )
            cur.execute('CREATE UNIQUE INDEX anchor ON searchIndex (name, type, path);')
            console.log('Created SQLite index')

            add_urls(docset_path_str, cur)
            db.commit()
    except Exception as exc:
        console.log('[bold red]Creating SQLite index failed.[/bold red]')
        clear_trash(docset_name)
        raise exc

    # Create the Info.plist File
    if not index_page:
        index_page = 'index.html'

    add_infoplist(info_path, index_page, docset_name)

    if icon is not None:
        icon_filename = pathlib.Path(icon)
        if icon_filename.suffix.lower() == '.png' and icon_filename.is_file():
            try:
                shutil.copy2(icon_filename, icon_path)
                console.log('Copied icon into docset')
            except Exception as exc:
                console.log('[bold red]Copying icon file failed.[/bold red]')
                clear_trash(docset_name)
                raise exc
        else:
            console.log('[bold red]Icon file should be a valid PNG image.[/bold red]')
            clear_trash(docset_name)
            raise ValueError('Icon file should be a valid PNG image.')
    console.log('Generated docset successfully')
