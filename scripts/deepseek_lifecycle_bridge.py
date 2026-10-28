"""Bounded stdin-only DeepSeek adapter; cloud processing belongs to the queue."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import akb
import lifecycle
import memory_sources

ROOT = Path(__file__).resolve().parents[1]
MAX_INPUT = 262144


def clean_events(events):
    rows = []
    if not isinstance(events, list):
        raise ValueError('invalid_events')
    for event in events[-128:]:
        if not isinstance(event, dict) or event.get('role') not in ('user', 'assistant'):
            continue
        text = event.get('text')
        if not isinstance(text, str):
            continue
        text = memory_sources._clean(text, role=event['role'])
        text = akb.redact_text(text[-18000:]).strip()
        if text:
            row = {'role': event['role'], 'text': text}
            if isinstance(event.get('id'), str) and re.fullmatch(r'seq:\d+', event['id']):
                row['id'] = event['id']
            rows.append(row)
    # Keep the final public dialogue window, never unbounded history.
    total = 0
    bounded = []
    for row in reversed(rows):
        available = 18000 - total
        if available <= 0:
            break
        row = dict(row, text=row['text'][-available:])
        bounded.append(row)
        total += len(row['text'])
    return list(reversed(bounded))


def dispatch(action, payload):
    cwd = payload.get('cwd')
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise ValueError('missing_workspace')
    cwd = str(Path(cwd).resolve())
    if action == 'recall':
        return {'context': lifecycle.recall(cwd)}
    if action != 'capture':
        raise ValueError('unknown_action')
    sid = payload.get('session_id')
    if not isinstance(sid, str) or not sid or len(sid) > 1024:
        raise ValueError('invalid_session')
    events = clean_events(payload.get('events'))
    if not events:
        return {'captured': False, 'reason': 'no_dialogue'}
    identity = hashlib.sha256(json.dumps([cwd, sid]).encode()).hexdigest()
    digest = hashlib.sha256(json.dumps(events, sort_keys=True).encode()).hexdigest()
    base = Path(os.environ.get('AKB_DISTILLER_STATE_DIR', str(ROOT / '.distiller'))) / 'deepseek-lifecycle'
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (base / (identity + '.lock')).open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = base / (identity + '.sha256')
        if marker.exists() and marker.read_text() == digest:
            return {'captured': False, 'reason': 'duplicate'}
        path = base / (identity + '.jsonl')
        native = []
        for row in events:
            message = {'role': row['role'], 'content': [{'type': 'text', 'text': row['text']}]}
            if row['role'] == 'user':
                native.append({'type': 'user/message', 'data': {
                    'source': {'kind': 'user'}, 'content': message['content']}})
            else:
                native.append({'type': 'assistant/message', 'data': {'message': message}})
            if 'id' in row:
                # Preserve the host sequence as its native numeric field. The
                # source reader derives stable record IDs from seq, not id.
                native[-1]['seq'] = int(row['id'].split(':', 1)[1])
        temp = path.with_suffix('.tmp')
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as handle:
            handle.write(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in native))
        temp.replace(path)
        result = lifecycle.capture('deepseek', {
            'cwd': cwd, 'session_id': sid, 'transcript_path': str(path),
        })
        if (result.get('captured') and result.get('queued')) or result.get('reason') == 'no_new_dialogue':
            marker.write_text(digest)
            os.chmod(marker, 0o600)
        return result


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError('input_limit')
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('invalid_payload')
        result = dispatch(sys.argv[1], payload)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # No payload, model text, credentials or exception message crosses stderr.
        print(json.dumps({'error': type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
