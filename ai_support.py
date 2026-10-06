"""Bounded DeepSeek context helpers; media bytes are request-only, never task data."""
from __future__ import annotations

import base64
import re
import shutil
from pathlib import Path


CHAT_FIELDS = {'message', 'history', 'allowedTools', 'executeTools', 'title', 'transport',
               'useKnowledge', 'documentIds', 'imageIds', 'videoIds', 'contextDevice'}
ASSIST_ACTIONS = {
    'polish': '润色资料，保留原意和事实，输出可直接编辑的 Markdown 草稿。',
    'summary': '总结资料，列出要点、结论和待确认事项。',
    'extract_memory': '提取值得长期保留的记忆，输出标题和 Markdown 草稿；不保存、不覆盖已有记忆。',
    'create_skill': '根据资料编写可复用 Skill 草稿，包含用途、触发条件、步骤和边界；不安装、不执行。',
    'plan_tasks': '将目标拆成可执行任务，列出先后顺序、依赖和验收条件；不创建计划任务。',
    'explain': '清楚解释资料或问题，给出必要的例子，标明不确定信息。',
    'code': '根据用户要求解释、审查或编写代码草稿，说明验证方法；工具执行只受本次显式授权控制。',
    'media': '分析用户选定的图片或视频截图，区分画面观察与推断；不能把截图分析称为完整视频或音频分析。',
}
SCRIPT_TOOL = 'devhelper_workflow_run_script'
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_CONTEXT_CHARS = 32000


def eligible_tool(name):
    """Do not expose credential, transport settings, routers or recursive AI tasks."""
    if name == SCRIPT_TOOL:
        return True
    lowered = name.lower()
    if lowered.startswith(('devhelper_workflow_', 'workflow_', 'deepseek_', 'ai_')):
        return False
    if lowered in {'devhelper_call_tool', 'devhelper_list_tools', 'research_call_tool'}:
        return False
    return not any(word in lowered for word in ('api_key', 'apikey', 'credential', 'password', 'secret',
        'relay_config', 'relay_pair', 'connection_config', 'transport_config', 'pairing_config', 'workflow_config'))


def executable(name, config):
    local = Path(config['localPython']).parent / name
    found = str(local) if local.is_file() and local.stat().st_mode & 0o111 else shutil.which(name)
    if found or name != 'ffmpeg':
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError):
        return None


def image_block(data):
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError('Image must contain between 1 byte and 5 MiB; resize it before retrying')
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 33 and data[12:16] == b'IHDR':
        mime = 'image/png'
    elif data.startswith(b'\xff\xd8\xff') and len(data) >= 12:
        mime = 'image/jpeg'
    elif data.startswith(b'RIFF') and len(data) >= 16 and data[8:12] == b'WEBP':
        mime = 'image/webp'
    elif data[:6] in (b'GIF87a', b'GIF89a') and len(data) >= 13:
        mime = 'image/gif'
    else:
        raise ValueError('Select a valid PNG, JPEG, WebP or GIF image')
    return {'type': 'image_url', 'image_url': {'url': 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')}}


def search_queries(message):
    queries = [message.strip()[:512]]
    words = re.findall(r'[A-Za-z0-9_\u4e00-\u9fff]{2,80}', message)
    # This store uses literal retrieval, never claims to create semantic vectors.
    for word in sorted(set(words), key=len, reverse=True):
        if word not in queries:
            queries.append(word)
        if len(queries) == 5:
            break
    return [query for query in queries if query]


def usage_record(value):
    usage = value.get('usage')
    if not isinstance(usage, dict):
        return {}
    return {key: item for key, item in usage.items()
            if re.fullmatch(r'[A-Za-z0-9_]+', key) and isinstance(item, (int, float, dict)) and not isinstance(item, bool)}
