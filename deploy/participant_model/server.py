"""Loopback-only CPU inference; accepts prepared features, never outcome data."""
import argparse
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


class Predictor:
    def __init__(self, directory):
        import catboost
        if catboost.__version__ != '1.2.10':
            raise ValueError('CatBoost 1.2.10 is required')
        directory = Path(directory)
        self.schema = json.loads((directory / 'feature-schema.json').read_text())
        self.metrics = json.loads((directory / 'metrics.json').read_text())
        self.sha = hashlib.sha256((directory / 'model.cbm').read_bytes()).hexdigest()
        self.schema_sha = hashlib.sha256(canonical(self.schema)).hexdigest()
        if (self.metrics.get('target') != 'participant_win.v1' or
                self.schema.get('target') != 'participant_win.v1' or
                self.sha != self.metrics.get('model_sha256') or
                self.schema_sha != self.metrics.get('schema_sha256')):
            raise ValueError('Model/schema identity or checksum mismatch')
        self.names = sum((self.schema[k] for k in ('categorical', 'numeric', 'missing_indicators')), [])
        if len(set(self.names)) != len(self.names):
            raise ValueError('Duplicate feature names')
        if self.schema['missing_indicators'] != [k + '__missing' for k in self.schema['numeric']]:
            raise ValueError('Unsupported missing indicators')
        self.model = catboost.CatBoostClassifier()
        self.model.load_model(str(directory / 'model.cbm'))
        if (self.model.feature_names_ != self.names or
                list(self.model.classes_) != [0, 1] or
                self.model.get_cat_feature_indices() != list(range(len(self.schema['categorical']))) or
                self.model.tree_count_ != self.metrics['tree_count']):
            raise ValueError('Model feature order, classes or tree count mismatch')
        self.pool = catboost.Pool

    def vector(self, features):
        if not isinstance(features, dict) or set(features) != set(self.names):
            raise ValueError('Exact feature schema required')
        for k in self.schema['categorical']:
            if not isinstance(features[k], str) or len(features[k]) > 2000:
                raise ValueError('Invalid categorical feature')
        if features['sport'] not in self.metrics['sports'] or features['selection_side'] not in ('home', 'away'):
            raise ValueError('Unsupported sport or participant')
        for k in self.schema['numeric']:
            value = features[k]
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError('Invalid numeric feature')
            missing = features[k + '__missing']
            if type(missing) is not int or missing != int(value is None):
                raise ValueError('Invalid missing indicator')
        return [features[k] if features[k] is not None else float('nan') for k in self.names]

    def predict(self, features):
        pool = self.pool([self.vector(features)], feature_names=self.names,
                         cat_features=self.schema['categorical'])
        probability = float(self.model.predict_proba(pool, thread_count=2)[0][1])
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError('Invalid model output')
        return {'format': 'prognozich.participant-prediction.v1', 'target': 'participant_win.v1',
                'model_sha256': self.sha, 'schema_sha256': self.schema_sha,
                'features_sha256': hashlib.sha256(canonical(features)).hexdigest(),
                'sport': features['sport'], 'selection_side': features['selection_side'],
                'market_scope': features['market_scope'], 'probability': probability,
                'experimental': True, 'holdout_verified': False}


def handler(predictor, token):
    slots = BoundedSemaphore(2)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass  # Never log credentials, input statistics or raw request URLs.

        def reply(self, status, value):
            body = canonical(value)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != '/health':
                return self.reply(404, {'error': 'not_found'})
            self.reply(200, {'state': 'ready', 'model_sha256': predictor.sha,
                             'target': 'participant_win.v1', 'device': 'CPU', 'catboost': '1.2.10'})

        def do_POST(self):
            if self.path != '/predict':
                return self.reply(404, {'error': 'not_found'})
            if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode()):
                return self.reply(401, {'error': 'unauthorized'})
            if not slots.acquire(blocking=False):
                return self.reply(503, {'error': 'busy'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536 or self.headers.get('Transfer-Encoding'):
                    return self.reply(413, {'error': 'invalid_body_size'})
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or set(data) != {'features', 'model_sha256'} or data['model_sha256'] != predictor.sha:
                    return self.reply(422, {'error': 'invalid_contract_or_model'})
                self.reply(200, predictor.predict(data['features']))
            except (ValueError, TypeError, KeyError, OverflowError, TimeoutError):
                self.reply(422, {'error': 'invalid_features'})
            except Exception:
                self.reply(500, {'error': 'inference_failed'})
            finally:
                slots.release()

    return Handler


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--model-dir', required=True)
    p.add_argument('--check', action='store_true')
    args = p.parse_args()
    predictor = Predictor(args.model_dir)
    if args.check:
        print(json.dumps({'state': 'verified', 'model_sha256': predictor.sha, 'features': len(predictor.names)}))
        return
    token = Path(os.environ['MODEL_TOKEN_FILE']).read_text().strip()
    if len(token) < 32:
        raise ValueError('Missing private API token')
    ThreadingHTTPServer(('127.0.0.1', 8090), handler(predictor, token)).serve_forever()


if __name__ == '__main__':
    main()
