"""Directory navigation over the asset catalog, including cloud-only projects.

This module never enumerates a source directory or opens an original file. Folder
rows are virtual groups of indexed paths, and ``childCount`` always counts the
recursive files matching the current kind filter (not the number of subfolders).
"""
from __future__ import annotations

import unicodedata
import uuid

try:
    from .asset_library import AssetLibraryError, KINDS, _id, _limit, _offset
except ImportError:
    from asset_library import AssetLibraryError, KINDS, _id, _limit, _offset


def _directory_path(value):
    """Accept a relative catalog directory; normalize only trailing slashes."""
    if not isinstance(value, str) or len(value) > 4096:
        raise AssetLibraryError('Use a relative catalog directory of at most 4096 characters')
    if value.startswith('/') or '\\' in value or ':' in value or any(
            unicodedata.category(char) == 'Cc' for char in value):
        raise AssetLibraryError('Use a relative catalog directory, not a local filesystem path')
    value = value.rstrip('/')
    if value and any(part in ('', '.', '..') for part in value.split('/')):
        raise AssetLibraryError('Catalog directories cannot contain empty, dot or parent segments')
    return value


def _like_prefix(value):
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def _breadcrumbs(project, path):
    if project is None:
        return []
    result = [{'name': project['name'], 'path': '', 'projectId': project['id']}]
    parts = path.split('/') if path else []
    for index, name in enumerate(parts):
        result.append({'name': name, 'path': '/'.join(parts[:index + 1]), 'projectId': project['id']})
    return result


def browse_assets(library, *, project_id=None, path='', kind=None, sort='name', order='asc', limit=100, offset=0):
    """Return one paginated directory without scanning or requiring source files.

    With no project and an empty path, rows are project folders. Inside a project,
    rows are immediate child folders and files. Folders precede files in either
    sort direction. Sort and pagination apply to the complete result. Unknown
    projects return 404; a missing/empty virtual directory returns an empty list.
    ``kind`` filters files and retains folders only when a matching descendant
    exists. A folder's ``childCount`` is its matching recursive file count.
    """
    path = _directory_path(path)
    limit, offset = _limit(limit, 500), _offset(offset)
    if project_id is not None:
        _id(project_id)
    elif path:
        raise AssetLibraryError('Choose a project before opening a catalog directory')
    if kind is not None and (not isinstance(kind, str) or kind not in KINDS):
        raise AssetLibraryError('Unknown asset kind')
    sort_columns = {'name': 'entry_name COLLATE NOCASE', 'modified': 'entry_modified',
                    'size': 'entry_size', 'kind': 'entry_kind COLLATE NOCASE'}
    if not isinstance(sort, str) or sort not in sort_columns or order not in ('asc', 'desc'):
        raise AssetLibraryError('Sort must be name/modified/size/kind with asc/desc order')
    ordering = (' ORDER BY is_dir DESC,' + sort_columns[sort] + ' ' + order.upper()
                + ',entry_name COLLATE NOCASE ASC,entry_id ASC')

    with library._db() as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        project = None
        if project_id is None:
            # Include empty projects normally; a kind filter retains matching
            # projects only, just as it does for virtual subfolders.
            kind_clause = ' AND a.kind=?' if kind else ''
            arguments = [kind] if kind else []
            cte = '''WITH entries AS (
                SELECT p.id AS entry_id,p.name AS entry_name,'folder' AS entry_kind,
                    NULL AS entry_size,COALESCE(MAX(a.mtime_ns)/1e9,p.updated) AS entry_modified,
                    COUNT(a.id) AS child_count,1 AS is_dir
                FROM projects p LEFT JOIN assets a ON a.project_id=p.id''' + kind_clause + '''
                GROUP BY p.id''' + (' HAVING COUNT(a.id)>0' if kind else '') + ')'
        else:
            project = db.execute('SELECT id,name FROM projects WHERE id=?', (project_id,)).fetchone()
            if project is None:
                raise AssetLibraryError('Project not found', 404)
            prefix = path + '/' if path else ''
            arguments = [len(prefix) + 1, project_id, _like_prefix(prefix), len(prefix), prefix]
            kind_clause = ' AND a.kind=?' if kind else ''
            if kind:
                arguments.append(kind)
            cte = '''WITH candidates AS (
                SELECT a.id,a.size,a.mtime_ns,a.kind,substr(a.path,?) AS relative_path
                FROM assets a WHERE a.project_id=? AND a.path LIKE ? ESCAPE '\\'
                    AND substr(a.path,1,?)=? COLLATE BINARY ''' + kind_clause + '''
            ), entries AS (
                SELECT substr(relative_path,1,instr(relative_path,'/')-1) AS entry_id,
                    substr(relative_path,1,instr(relative_path,'/')-1) AS entry_name,
                    'folder' AS entry_kind,NULL AS entry_size,MAX(mtime_ns)/1e9 AS entry_modified,
                    COUNT(*) AS child_count,1 AS is_dir
                FROM candidates WHERE instr(relative_path,'/')>0 GROUP BY entry_name
                UNION ALL
                SELECT id AS entry_id,relative_path AS entry_name,kind AS entry_kind,
                    size AS entry_size,mtime_ns/1e9 AS entry_modified,0 AS child_count,0 AS is_dir
                FROM candidates WHERE instr(relative_path,'/')=0
            )'''
        total = db.execute(cte + ' SELECT COUNT(*) FROM entries', arguments).fetchone()[0]
        rows = db.execute(cte + ' SELECT * FROM entries' + ordering + ' LIMIT ? OFFSET ?',
                          [*arguments, limit, offset]).fetchall()
        items = []
        for row in rows:
            if row['is_dir']:
                folder_project_id = project_id or row['entry_id']
                folder_path = '/'.join(filter(None, (path, row['entry_name']))) if project else ''
                identifier = ('folder:' + str(uuid.uuid5(uuid.UUID(folder_project_id), folder_path))
                              if project else 'project:' + folder_project_id)
                items.append({'id': identifier, 'type': 'folder', 'isDir': True, 'kind': 'folder',
                    'name': row['entry_name'], 'projectId': folder_project_id,
                    'projectName': project['name'] if project else row['entry_name'],
                    'path': folder_path, 'modifiedAt': row['entry_modified'], 'bytes': None,
                    'childCount': row['child_count'], 'childCountMode': 'recursive-files'})
            else:
                asset_row = db.execute('SELECT * FROM assets WHERE id=?', (row['entry_id'],)).fetchone()
                item = library._public_asset(db, asset_row)
                item.update(type='file', isDir=False, projectName=project['name'])
                items.append(item)
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset, 'sort': sort,
                'order': order, 'mode': 'browse', 'projectId': project_id, 'path': path,
                'breadcrumbs': _breadcrumbs(project, path), 'childCountMode': 'recursive-files'}
