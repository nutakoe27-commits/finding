"""Administrative login must not expose credentials or mutate before preflight."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import prepare_mysql as preparation


class PrepareMySQLTests(unittest.TestCase):
    def test_password_is_private_temporary_and_not_in_process_arguments(self):
        secret='fixture-password-\\"#\t'
        client=preparation.MySQLAdmin('/usr/bin/mysql','db_admin',secret)
        path=Path(client.command[1].split('=',1)[1])
        try:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode),0o700)
            self.assertTrue(path.read_text().startswith('[client]\npassword="'))
            self.assertNotIn(secret,' '.join(client.command))
            with patch.object(preparation.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'1\n','')) as run:
                self.assertEqual(client.execute('SELECT 1;','проверка входа'),'1')
            self.assertNotIn('env',run.call_args.kwargs)
            self.assertNotIn(secret,repr(run.call_args))
            self.assertIn('--user=db_admin',client.command)
        finally:client.close()
        self.assertFalse(path.exists())

    def test_error_code_and_stage_are_visible_but_raw_stderr_is_not(self):
        for password in (None,'fixture-root-password'):
            with self.subTest(password_supplied=password is not None):
                client=preparation.MySQLAdmin('/usr/bin/mysql',password=password)
                try:
                    with patch.object(preparation.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'','ERROR 1045 (28000): SECRET_ROOT_PASSWORD')):
                        with self.assertRaises(ValueError) as error:client.execute('SELECT 1;','проверка входа')
                    self.assertIn('1045',str(error.exception));self.assertIn('проверка входа',str(error.exception))
                    self.assertNotIn('SECRET_ROOT_PASSWORD',str(error.exception))
                    self.assertIn('--admin-user' if password else '--ask-admin-password',str(error.exception))
                finally:client.close()

    def test_rejected_initial_login_never_creates_settings_or_runs_ddl(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(preparation.Settings,'from_env',return_value=SimpleNamespace(data_dir=Path(folder))), \
                 patch.object(preparation.shutil,'which',return_value='/usr/bin/mysql'), \
                 patch.object(preparation.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'','ERROR 1045 (28000): fixture-secret')) as run, \
                 patch.object(preparation.sys,'argv',['prepare_mysql.py']), \
                 patch.object(preparation.secrets,'token_urlsafe',side_effect=AssertionError('must not create application credentials')):
                with self.assertRaises(SystemExit) as error:preparation.main()
            self.assertIn('1045',str(error.exception));self.assertNotIn('fixture-secret',str(error.exception))
            self.assertEqual(run.call_count,1)
            self.assertNotIn('CREATE ',run.call_args.kwargs['input'])
            self.assertFalse((Path(folder)/'mysql-target.env').exists())

    def test_password_login_creates_only_application_credentials_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as folder:
            root_secret='fixture-administrator-password';app_secret='fixture-application-password';calls=[]
            def execute(command,**kwargs):
                path=Path(command[1].split('=',1)[1]);self.assertTrue(path.is_file())
                self.assertIn(root_secret,path.read_text());self.assertNotIn(root_secret,repr(command))
                self.assertNotIn(root_secret,kwargs['input']);calls.append((command,kwargs))
                return subprocess.CompletedProcess(command,0,'0\n0\n' if len(calls)==1 else '','')
            output=StringIO()
            with patch.object(preparation.Settings,'from_env',return_value=SimpleNamespace(data_dir=Path(folder))), \
                 patch.object(preparation.shutil,'which',return_value='/usr/bin/mysql'), \
                 patch.object(preparation.getpass,'getpass',return_value=root_secret), \
                 patch.object(preparation.secrets,'token_urlsafe',return_value=app_secret), \
                 patch.object(preparation,'server_check',return_value={'version':'8.0.46'}), \
                 patch.object(preparation.subprocess,'run',side_effect=execute), \
                 patch.object(preparation.sys,'argv',['prepare_mysql.py','--ask-admin-password']),redirect_stdout(output):
                preparation.main()
            pending=Path(folder)/'mysql-target.env'
            self.assertIn(app_secret,pending.read_text());self.assertNotIn(root_secret,pending.read_text())
            self.assertNotIn(root_secret,output.getvalue());self.assertNotIn(app_secret,output.getvalue())
            self.assertEqual(stat.S_IMODE(pending.stat().st_mode),0o600)
            self.assertEqual(len(calls),2);self.assertIn("CREATE USER 'matchlab_app'@'127.0.0.1'",calls[1][1]['input'])
            self.assertNotIn('ALTER USER',calls[1][1]['input'])
            self.assertFalse(Path(calls[0][0][1].split('=',1)[1]).exists())


if __name__=='__main__':unittest.main()
