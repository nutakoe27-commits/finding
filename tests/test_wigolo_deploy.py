import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'install_wigolo_tunnel.py'
spec = importlib.util.spec_from_file_location('install_wigolo_tunnel', PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SOURCE = '''# Managed by Matchlab server scraper
[Service]
User=operator
ExecStart=/usr/bin/ssh -NT -i /home/operator/.ssh/key -o IdentitiesOnly=yes -o BatchMode=yes -o IdentityAgent=none -o StrictHostKeyChecking=yes -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=10 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:18081:127.0.0.1:8080 root@vps.example
'''


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.source, self.target, self.token = base/'source', base/'target', base/'token'
        self.source.write_text(SOURCE)
        self.token.write_text('a'*64+'\n')
        self.patches = [patch.object(module, 'SOURCE', self.source), patch.object(module, 'TARGET', self.target),
                        patch.object(module, 'TOKEN', self.token), patch.object(module.os, 'geteuid', return_value=0),
                        patch.object(module.sys, 'argv', ['install_wigolo_tunnel.py'])]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_tunnel_is_separate_and_token_goes_only_via_stdin(self):
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            return type('Result', (), {'returncode': 0, 'stdout': ''})()
        with patch.object(module.subprocess, 'run', side_effect=run):
            module.main()
        unit = self.target.read_text()
        self.assertIn('-R 127.0.0.1:13333:127.0.0.1:13334', unit)
        self.assertNotIn('-L 127.0.0.1:18081', unit)
        self.assertNotIn('a'*64, unit)
        transfer = next(c for c in calls if c[0][0] == 'runuser')
        self.assertEqual(transfer[1]['input'], b'a'*64+b'\n')
        self.assertNotIn('a'*64, str(transfer[0]))
        self.assertEqual(self.source.read_text(), SOURCE)

    def test_unexpected_existing_tunnel_is_rejected_without_writes(self):
        self.source.write_text(SOURCE.replace('StrictHostKeyChecking=yes', 'StrictHostKeyChecking=no'))
        with patch.object(module.subprocess, 'run', return_value=type('Result', (), {'stdout': ''})()), self.assertRaises(SystemExit):
            module.main()
        self.assertFalse(self.target.exists())
