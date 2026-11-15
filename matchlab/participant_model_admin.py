"""Admin-only view of an isolated, read-only prospective experiment report."""
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from . import accounts


def report(path):
    if not path:
        return {'state': 'not_configured'}
    try:
        file = Path(path)
        if file.is_symlink() or file.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('Invalid report file')
        with file.open('rb') as stream:
            data = json.loads(stream.read(4 * 1024 * 1024 + 1))
        if data.get('format') != 'prognozich.model-admin.v1':
            raise ValueError('Unknown report')
        result = {k: data.get(k) for k in ('state', 'checked_at', 'model_sha256', 'target',
            'training_events', 'training_rows', 'lead_minutes', 'recorded_events', 'scored_events',
            'pending_events', 'evaluation', 'skipped', 'unscored_reasons', 'inference_error', 'error_type')}
        result['matches'] = [{k: item.get(k) for k in ('event', 'issued_at', 'sample_as_of',
            'scope', 'market', 'state', 'winner', 'reason', 'probabilities')} for item in data.get('matches', [])[:100]]
        checked = datetime.fromisoformat(data['checked_at'])
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        result.update(stale=age > 300 or age < -60, experimental=True, mode='prospective', quality_verified=False)
        return result
    except FileNotFoundError:
        return {'state': 'waiting'}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {'state': 'error', 'error_type': 'report_unavailable'}


def install(app, repo, settings):
    @app.get('/api/admin/participant-model')
    def participant_model(request: Request):
        accounts.admin_user(request)
        return JSONResponse(report(settings.participant_model_report), headers={'Cache-Control': 'private, no-store'})
