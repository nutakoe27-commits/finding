"""Targeted isolated API tests; no website database, payments or GPU."""
import importlib.util
import json
import os
from pathlib import Path
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location('participant_server', Path(__file__).resolve().parents[1] / 'deploy/participant_model/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


@unittest.skipUnless(os.environ.get('PARTICIPANT_MODEL_BUNDLE'), 'Requires supplied model bundle and dedicated CatBoost environment')
class ModelServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = Path(os.environ['PARTICIPANT_MODEL_BUNDLE'])
        cls.predictor = server.Predictor(cls.bundle / 'model')
        with (cls.bundle / 'dataset/rows.jsonl').open() as f:
            cls.features = json.loads(next(f))['features']

    def test_missing_values_and_exact_order(self):
        features = dict(reversed(list(self.features.items())))
        original = self.predictor.predict(self.features)
        self.assertEqual(self.predictor.predict(features), original)
        self.assertTrue(0 <= original['probability'] <= 1)
        name = self.predictor.schema['numeric'][0]
        features[name] = None
        features[name + '__missing'] = 1
        self.assertTrue(0 <= self.predictor.predict(features)['probability'] <= 1)

    def test_unknown_fields_types_and_bad_indicators_rejected(self):
        name = self.predictor.schema['numeric'][0]
        for changed in ({'target': 1}, {name: True}, {name: float('nan')},
                        {name: None, name + '__missing': 0}, {'sport': 'unknown'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.predictor.predict(self.features | changed)

    def test_training_metrics_reproduced(self):
        import math
        rows = [json.loads(line) for line in (self.bundle / 'dataset/rows.jsonl').read_text().splitlines()]
        rows = [r for r in rows if r['target'] is not None]
        pool = self.predictor.pool([self.predictor.vector(r['features']) for r in rows],
            feature_names=self.predictor.names, cat_features=self.predictor.schema['categorical'])
        probs = self.predictor.model.predict_proba(pool, thread_count=2)[:, 1]
        loss = -sum(r['target'] * math.log(max(1e-15, p)) + (1-r['target']) * math.log(max(1e-15, 1-p)) for r,p in zip(rows,probs))/len(rows)
        self.assertAlmostEqual(loss, self.predictor.metrics['evaluation']['training']['overall']['log_loss'], places=12)

    def test_http_auth_contract_and_probability(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.handler(self.predictor, 'a'*64))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = 'http://127.0.0.1:' + str(httpd.server_port)
            with urlopen(base+'/health') as r:
                self.assertEqual(json.load(r)['state'], 'ready')
            body = {'features': self.features, 'model_sha256': self.predictor.sha}
            for token, data, expected in [('', body, 401), ('a'*64, body | {'target': 1}, 422),
                    ('a'*64, body | {'model_sha256': 'wrong'}, 422), ('a'*64, body, 200)]:
                request = Request(base+'/predict', data=json.dumps(data).encode(), headers={'Authorization': 'Bearer '+token})
                try:
                    with urlopen(request) as response:
                        self.assertEqual(response.status, expected)
                        self.assertEqual(json.load(response)['model_sha256'], self.predictor.sha)
                except HTTPError as e:
                    self.assertEqual(e.code, expected)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
