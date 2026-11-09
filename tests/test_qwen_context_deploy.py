"""Context rollout must preserve the shared model and refuse stale or foreign changes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import configure_qwen_context as deploy


SOURCE = b'''[Unit]
Description=Shared model used by two projects
[Service]
User=model
EnvironmentFile=/etc/private-model.env
ExecStart=/srv/model/bin/python -m vllm.entrypoints.openai.api_server \\
  --model mattbucci/Qwen3.8-27B-AWQ \\
  --served-model-name QuantTrio/Qwen3.5-35B-A3B-AWQ \\
  --api-key private-fixture-key --port 8000 \\
  --quantization awq --dtype float16 --gpu-memory-utilization 0.82 \\
  --max-model-len 32768 --tensor-parallel-size 2 --max-num-seqs 4
Restart=on-failure
[Install]
WantedBy=multi-user.target
'''


class ContextDeploymentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.system = self.root / 'system'; self.system.mkdir()
        self.fragment = self.system / 'vllm.service'; self.fragment.write_bytes(SOURCE)
        self.state = self.root / 'private'
        self.calls = []; self.dropins = ''
        for name, value in (('STATE', self.state), ('SYSTEM', self.system)):
            change = patch.object(deploy, name, value); change.start(); self.addCleanup(change.stop)
        self.destination = self.system / 'vllm.service.d' / deploy.DROPIN

    def call(self, args):
        self.calls.append(args)
        if args[:2] == ['systemctl', 'show']:
            return f'LoadState=loaded\nFragmentPath={self.fragment}\nDropInPaths={self.dropins}\n'
        return ''

    def plan(self):
        return deploy.prepare('vllm.service', 131072, self.call)

    def test_prepare_keeps_running_configuration_and_protects_private_backups(self):
        result = self.plan(); path = Path(result['plan'])
        self.assertEqual(self.fragment.read_bytes(), SOURCE)
        self.assertFalse(self.destination.exists())
        self.assertFalse(result['restart_performed'])
        self.assertFalse(result['kv_capacity_verified'])
        self.assertNotIn('private-fixture-key', json.dumps(result))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((path.parent / 'original.service').read_bytes(), SOURCE)
        self.assertEqual((path.parent / 'candidate/vllm.service').read_bytes(), SOURCE.replace(b'32768', b'131072'))
        self.assertTrue(any(c[:2] == ['systemd-analyze', 'verify'] for c in self.calls))
        self.assertFalse(any('restart' in c or 'daemon-reload' in c for c in self.calls))

    def test_install_and_rollback_touch_only_the_owned_override_and_never_restart(self):
        plan = Path(self.plan()['plan'])
        result = deploy.apply(plan, self.call)
        self.assertTrue(result['restart_required'])
        self.assertEqual(self.fragment.read_bytes(), SOURCE)
        expected = SOURCE.split(b'ExecStart=', 1)[1].split(b'Restart=', 1)[0].replace(b'32768', b'131072')
        self.assertEqual(self.destination.read_bytes(), b'[Service]\nExecStart=\nExecStart=' + expected)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        foreign = self.destination.parent / '99-other-project.conf'
        foreign.write_text('[Service]\nNice=1\n')
        deploy.rollback(plan, self.call)
        self.assertFalse(self.destination.exists())
        self.assertTrue(foreign.exists())
        self.assertEqual(self.fragment.read_bytes(), SOURCE)
        self.assertFalse(any('restart' in c or 'stop' in c or 'start' in c for c in self.calls))

    def test_changed_unit_or_new_dropin_prevents_installation(self):
        plan = Path(self.plan()['plan'])
        self.fragment.write_bytes(SOURCE + b'\n# edit by another administrator\n')
        with self.assertRaises(ValueError): deploy.apply(plan, self.call)
        self.assertFalse(self.destination.exists())
        self.fragment.write_bytes(SOURCE)
        self.dropins = '/etc/systemd/system/vllm.service.d/other.conf'
        with self.assertRaises(ValueError): deploy.apply(plan, self.call)
        self.assertFalse(self.destination.exists())

    def test_modified_override_is_not_deleted_during_rollback(self):
        plan = Path(self.plan()['plan']); deploy.apply(plan, self.call)
        self.destination.write_bytes(self.destination.read_bytes() + b'\n# manual edit\n')
        with self.assertRaises(ValueError): deploy.rollback(plan, self.call)
        self.assertTrue(self.destination.exists())

    def test_modified_prepared_artifact_is_not_installed(self):
        plan = Path(self.plan()['plan'])
        (plan.parent / 'dropin.conf').write_bytes(b'[Service]\nExecStart=/unexpected-command\n')
        with self.assertRaises(ValueError): deploy.apply(plan, self.call)
        self.assertFalse(self.destination.exists())

    def test_occupied_override_is_never_replaced(self):
        plan = Path(self.plan()['plan'])
        self.destination.parent.mkdir()
        self.destination.write_bytes(b'[Service]\nNice=5\n')
        with self.assertRaises(FileExistsError): deploy.apply(plan, self.call)
        self.assertEqual(self.destination.read_bytes(), b'[Service]\nNice=5\n')
        self.assertEqual(self.fragment.read_bytes(), SOURCE)

    def test_failed_write_never_exposes_partial_override(self):
        plan = Path(self.plan()['plan'])
        with patch.object(deploy.os, 'fsync', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError): deploy.apply(plan, self.call)
        self.assertFalse(self.destination.exists())

    def test_reload_failure_removes_just_installed_override(self):
        plan = Path(self.plan()['plan']); attempted = []
        def fail_once(args):
            if args == ['systemctl', 'daemon-reload']:
                attempted.append(args)
                if len(attempted) == 1: raise ValueError('fixture failure')
            return self.call(args)
        with self.assertRaises(ValueError): deploy.apply(plan, fail_once)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.fragment.read_bytes(), SOURCE)

    def test_preexisting_overrides_and_unsupported_launches_are_not_rewritten(self):
        self.dropins = '/etc/systemd/system/vllm.service.d/override.conf'
        with self.assertRaises(ValueError): self.plan()
        self.assertFalse(self.state.exists())
        for command in ('/srv/vllm-wrapper.sh', '/bin/sh -c "vllm serve example --max-model-len 32768"',
                        '/srv/python -m vllm --max-model-len $MODEL_CONTEXT',
                        '/srv/python -m vllm --max-model-len 32768 --max-model-len 32768'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                deploy.changed_start(('[Service]\nExecStart=' + command + '\n').encode(), 131072)

    def test_equal_syntax_and_quoted_numeric_values_preserve_other_arguments(self):
        for flag in ('--max-model-len=32768', '--max-model-len "32768"'):
            source = ('[Service]\nExecStart=/srv/vllm --served-model-name alias ' + flag + ' --port 8000\n').encode()
            old, candidate, _ = deploy.changed_start(source, 131072)
            self.assertEqual(old, 32768)
            self.assertEqual(candidate, source.replace(b'32768', b'131072'))


if __name__ == '__main__':
    unittest.main()
