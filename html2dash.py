import os
import pathlib
import re
import sqlite3
import urllib.parse

import yaml
from lxml import html


def yaml2sqlite(yaml_config_file, sqlite_db):
    db = sqlite3.connect(sqlite_db)
    cur = db.cursor()
    cur.execute(
        '''
    DROP TABLE IF EXISTS searchIndex;
'''
    )

    cur.execute(
        '''
        CREATE TABLE
            searchIndex(id INTEGER PRIMARY KEY,
                        name TEXT,
                        type TEXT,
                        path TEXT);
    '''
    )

    cur.execute(
        '''
        CREATE UNIQUE INDEX
            anchor
        ON
            searchIndex (name, type, path);
    '''
    )

    db.commit()

    with open(yaml_config_file) as fpt:
        data = yaml.load(fpt, Loader=yaml.Loader)
    print(data.keys())

    if data:
        prev_title = ''
        for page in data.get('nav', []):
            print(page)
            mdpath, title = page[0], '-'.join(page[1:])
            if '**HIDDEN**' not in title:
                if 'index.md' in mdpath:
                    htmlpath = mdpath.replace('index.md', 'index.html')
                else:
                    htmlpath = mdpath.replace('.md', '/index.html')

                if '&blacksquare;' in title:
                    title = re.sub('.*&blacksquare;&nbsp;\s*', prev_title + ' - ', title)
                else:
                    prev_title = title
                cur.execute(
                    '''
                INSERT OR IGNORE INTO
                    searchIndex(name, type, path)
                VALUES
                    (?, ?, ?);
                ''',
                    (title, 'Guide', htmlpath),
                )
                db.commit()
                print(
                    f'Added the following entry to {sqlite_db}\n\tname: {title}\n\ttype: Guide\n\tpath: {htmlpath}'
                )

    db.close()


def abs2rel_func(link):
    if link[:2] == "//":
        newlink = f'https:{link}'
    elif link[:1] == "/":
        relpath = os.path.relpath(path, root)
        newlink = f'{relpath}/{link}'
    else:
        newlink = link

    print(f'old link: {link} ---> new link: {newlink}')
    return newlink


def dashrepl(match):
    (hopen, id, name, hclose) = match.group(1, 2, 3, 4)
    dashname = name
    dashname = re.sub('<.*?>', '', dashname)
    dashname = re.sub('[^a-zA-Z0-9\.\(\)\?\',:; ]', '-', dashname)
    dashname = urllib.parse.quote(dashname)
    dash = f'<a name="//apple_ref/cpp/Section/{dashname}" class="dashAnchor"></a>'
    header = f'<h{hopen} id="{id}">{name}</h{hclose}>'
    return f'{dash}\n{header}'


def add_dash_anchors(path):
    for root, dirs, files in os.walk(path):
        root = pathlib.Path(root)
        for file in files:
            if file.find(suffix) != -1:
                file = root / file
                with open(file) as fpt:
                    page = fpt.read()
                try:
                    html_content = re.sub('<h([1-2]) id="(.*?)">(.*?)</h([1-2])>', dashrepl, page)
                    with open(file, 'w') as fpt:
                        fpt.write(html_content)
                        print(f'file: {file}')
                except Exception as exc:
                    print(f'error: {exc}, file: {file}')


path = '/Users/abanihi/devel/personal/dash-docsets/fastapi.docset/Contents/Resources/Documents'
suffix = '.html'
# add_dash_anchors(path)
yaml2sqlite(
    yaml_config_file='/var/folders/z7/sdhzbbr96bv2wjrsb92qsm3dwz5p3x/T/fastapi/docs/en/mkdocs.yml',
    sqlite_db='/Users/abanihi/devel/personal/dash-docsets/fastapi.docset/Contents/Resources/docSet.dsidx',
)

# for root, dirs, files in os.walk(path):
#     root = pathlib.Path(root)
#     for file in files:
#         if file.find(suffix) != -1:
#             file = root/file
#             with open(file) as fpt:
#                 page = fpt.read()
#             try:
#                 html_content = html.fromstring(page)
#                 html_content.rewrite_links(abs2rel_func)
#             except Exception as exc:
#                 print(f'error: {exc}')
