import io
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from scripts import configure_metrika_goals as config


class GoalSetupTests(unittest.TestCase):
    def test_existing_and_conflicting_goals_are_never_replaced(self):
        identifier,name=config.GOALS[0]
        existing=[{'name':'Другое имя','type':'action','conditions':[{'type':'exact','url':identifier}]},
                  {'name':config.GOALS[1][1],'type':'url','conditions':[]}]
        rows=config.plan(existing)
        self.assertEqual(rows[0][2],'exists')
        self.assertEqual(rows[1][2],'conflict')
        self.assertTrue(all(row[2]=='create' for row in rows[2:]))
        existing[0]['status']='disabled'
        self.assertEqual(config.plan(existing)[0][2],'review')

    def test_preview_is_read_only_and_apply_creates_idempotent_js_goals(self):
        saved=[]; calls=[];secret='fixture-secret-never-print'
        def fake_request(opener,token,method,body=None):
            self.assertEqual(token,secret);calls.append(method)
            if method=='GET':return {'goals':list(saved)}
            goal=body['goal'];self.assertEqual(goal['type'],'action')
            self.assertEqual(goal['conditions'][0]['type'],'exact')
            saved.append({**goal,'id':len(saved)+1});return {'goal':saved[-1]}
        output=io.StringIO()
        with patch.object(config.sys.stdin,'isatty',return_value=True),patch.object(config.getpass,'getpass',return_value=secret),patch.object(config,'request',side_effect=fake_request),redirect_stdout(output):
            self.assertEqual(config.main([]),0)
            self.assertEqual(calls,['GET'])
            self.assertEqual(config.main(['--apply']),0)
            self.assertEqual(len(saved),len(config.GOALS))
            self.assertEqual(config.main(['--apply']),0)
        self.assertEqual(calls.count('POST'),len(config.GOALS))
        self.assertNotIn(secret,output.getvalue())

    def test_error_does_not_print_secret_or_server_body(self):
        secret='fixture-token-never-print'; output=io.StringIO()
        with patch.object(config.sys.stdin,'isatty',return_value=True),patch.object(config.getpass,'getpass',return_value=secret),patch.object(config,'request',side_effect=ValueError(secret)),redirect_stderr(output):
            self.assertEqual(config.main(['--apply']),1)
        self.assertNotIn(secret,output.getvalue())


if __name__=='__main__':unittest.main()
