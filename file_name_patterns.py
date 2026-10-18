"""Literal, bounded batch-rename tokens; never evaluate a filename template."""
from pathlib import PurePosixPath
import re
import unicodedata


def renamed_name(template, basename, is_dir, index, total):
    if not isinstance(template, str) or not 1 <= len(template) <= 240:
        raise ValueError('请填写有效的名称或重命名模板（最多 240 个字符）')
    if total > 1 and '{index}' not in template and '{name}' not in template:
        raise ValueError('批量重命名请使用 {index} 或 {name} 区分项目')
    path = PurePosixPath(basename)
    extension = '' if is_dir else path.suffix
    stem = basename[:-len(extension)] if extension else basename
    result = template.replace('{index}', str(index)).replace('{name}', stem).replace('{ext}', extension)
    if '{' in result or '}' in result:
        raise ValueError('模板仅支持 {index}、{name} 和 {ext}')
    if (not result or len(result) > 240 or result in ('.', '..') or result.endswith((' ', '.'))
            or re.search(r'[/\\:*?"<>|]', result)
            or any(unicodedata.category(char) == 'Cc' for char in result)):
        raise ValueError('生成的名称无效，请检查名称长度、路径字符和结尾')
    return result
