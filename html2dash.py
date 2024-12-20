#! /usr/bin/env python3

import contextlib
import os
import pathlib
import re
import sqlite3
import subprocess

from bs4 import BeautifulSoup


def update_db(name, path, cur):
    with contextlib.suppress(Exception):
        cur.execute('SELECT rowid FROM searchIndex WHERE path = ?', (path,))
        dbpath = cur.fetchone()
        cur.execute('SELECT rowid FROM searchIndex WHERE name = ?', (name,))
        dbname = cur.fetchone()

        if dbpath is None and dbname is None:
            cur.execute(
                'INSERT OR IGNORE INTO searchIndex(name, type, path)\
                    VALUES (?,?,?)',
                (name, 'Section', path),
            )


def add_urls(docset_path, cur):
    index_page = open(os.path.join(docset_path, 'index.html')).read()
    soup = BeautifulSoup(index_page, 'html.parser')
    any_regex = re.compile('.*')
    for tag in soup.find_all('a', {'href': any_regex}):
        name = tag.text.strip()
        if len(name) > 0:
            path = tag.attrs['href'].strip()
            if path.split('#')[0] not in ('index.html'):
                update_db(name, path, cur)


def add_infoplist(info_path, index_page, docset_name):
    info_path = pathlib.Path(info_path)
    name = docset_name.split('.')[0]
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
        info_path.write_text(info)
        print('Create the Info.plist File')
    except Exception as exc:
        print('**Error**:  Create the Info.plist File Failed..')
        clear_trash(docset_name)
        raise exc


def clear_trash(docset_name):
    try:
        subprocess.call(['rm', '-r', docset_name])
        print('Clear generated useless files!')
    except Exception as exc:
        print('**Error**:  Clear trash failed...')
        raise exc


def dash_webgen(name: str = None, url: str = None, destination: str | pathlib.Path = None) -> None:
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
    subprocess.call(command)


def custom_builder(
    name: str = None,
    destination: str | pathlib.Path = None,
    icon: str | pathlib.Path = None,
    index_page: str = None,
    source: str | pathlib.Path = None,
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

    source_dir = source
    if source_dir[-1] == '/':
        source_dir = source[:-1]

    source_dir = pathlib.Path(source_dir)
    source_dir.mkdir(parents=True, exist_ok=True)
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
    print(f'The {docset_path} Docset Folder is ready!')

    sqlite_path = (dest_path / dsidx_path).as_posix()
    info_path = (dest_path / info).as_posix()
    icon_path = (dest_path / icon_path).as_posix()

    docset_path = docset_path.as_posix()
    source_dir = source_dir.as_posix()
    docset_name = docset_name.as_posix()

    # Copy the HTML Documentation to the Docset Folder
    try:
        ignored = {'.git/', '.doctrees/', '_sources/', '*/*.ipynb'}
        excludes = [f'--exclude={pat}' for pat in ignored]
        arg_list = (
            ['rsync', '-avhr', '--progress']
            + excludes
            + [f'{source_dir}/{f}' for f in os.listdir(source_dir) if f not in ignored]
        ) + [docset_path]

        subprocess.call(arg_list)
        print('Copy the HTML Documentation!')
    except Exception as exc:
        print('**Error**:  Copy Html Documents Failed...')
        clear_trash(docset_name)
        raise exc

    # create and connect to SQLite
    try:
        db = sqlite3.connect(sqlite_path)
        cur = db.cursor()
    except Exception as exc:
        print('**Error**:  Create SQLite Index Failed...')
        clear_trash(docset_name)
        raise exc

    with contextlib.suppress(Exception):
        cur.execute('DROP TABLE searchIndex;')
    cur.execute(
        'CREATE TABLE searchIndex(id INTEGER PRIMARY KEY,\
                name TEXT,\
                type TEXT,\
                path TEXT);'
    )
    cur.execute('CREATE UNIQUE INDEX anchor ON searchIndex (name, type, path);')
    print('Create the SQLite Index')

    add_urls(docset_path, cur)
    db.commit()
    db.close()

    # Create the Info.plist File
    if not index_page:
        index_page = 'index.html'

    add_infoplist(info_path, index_page, docset_name)

    if icon_filename := str(icon):
        if icon_filename.endswith('.png') and os.path.isfile(icon_filename):
            try:
                subprocess.call(['cp', icon_filename, icon_path])
                print('Created the Icon for the Docset!')
            except Exception as exc:
                print('**Error**:  Copy Icon file failed...')
                clear_trash(docset_name)
                raise exc
        else:
            print('**Error**:  Icon file should be a valid PNG image...')
            clear_trash(docset_name)
            exit(2)
    print('Generate Docset Successfully!')
