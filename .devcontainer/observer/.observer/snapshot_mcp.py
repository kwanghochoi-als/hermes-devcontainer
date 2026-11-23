"""Stdio MCP adapter for the observer's session-bound Git object reader."""

import json
import os
from pathlib import Path
import subprocess
import sys

# The launcher uses Python isolated mode. Import only from the mounted policy.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate


TOOL = {
    'name': 'read_snapshot',
    'description': 'List tracked paths in a verified repository, or supply path to read a file at its pinned commit.',
    'inputSchema': {
        'type': 'object',
        'properties': {
            'snapshot': {'type': 'string', 'pattern': '^[0-9a-f]{32}$'},
            'repository': {'type': 'string', 'enum': sorted(gate.REPOS)},
            'path': {'type': 'string', 'description': 'Exact tracked file path. Omit to list paths.'},
        },
        'required': ['snapshot', 'repository'], 'additionalProperties': False,
    },
    'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False},
}


def read_for_run(arguments):
    selection = gate.mcp_request(arguments)
    receipt_path = os.environ.get('HERMES_OBSERVER_RECEIPT')
    if not selection or not receipt_path:
        raise ValueError('unverified request')
    receipt = json.loads(Path(receipt_path).read_text())
    if not isinstance(receipt, dict) or not isinstance(receipt.get('session_id'), str) or not receipt['session_id']:
        raise ValueError('missing session')
    commits = receipt.get('repositories')
    if not isinstance(commits, dict) or set(commits) != set(gate.REPOS) or gate.key(receipt).read_text() != selection[0]:
        raise ValueError('snapshot does not belong to this session')
    return gate.read_snapshot(selection, expected_commits=commits).decode('utf-8', errors='replace')


def reply(request):
    if not isinstance(request, dict) or request.get('jsonrpc') != '2.0':
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid request'}}
    if 'id' not in request:
        return None
    response = {'jsonrpc': '2.0', 'id': request['id']}
    method = request.get('method')
    params = request.get('params', {})
    if not isinstance(params, dict):
        return dict(response, error={'code': -32602, 'message': 'Invalid parameters'})
    if method == 'initialize':
        versions = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
        version = params.get('protocolVersion')
        result = {'protocolVersion': version if version in versions else versions[0],
                  'capabilities': {'tools': {}}, 'serverInfo': {'name': 'project_snapshot', 'version': '1.0'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [TOOL]}
    elif method == 'tools/call' and params.get('name') == TOOL['name']:
        try:
            content = read_for_run(params.get('arguments'))
        except (ValueError, OSError, KeyError, subprocess.SubprocessError):
            result = {'isError': True, 'content': [{'type': 'text', 'text': 'This snapshot or file is unavailable for this run.'}]}
        else:
            result = {'content': [{'type': 'text', 'text': content}]}
    else:
        return dict(response, error={'code': -32602 if method == 'tools/call' else -32601,
                                     'message': 'Unknown tool or method'})
    return dict(response, result=result)


if __name__ == '__main__':
    for line in sys.stdin:
        try:
            response = reply(json.loads(line))
        except json.JSONDecodeError:
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Invalid JSON'}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
