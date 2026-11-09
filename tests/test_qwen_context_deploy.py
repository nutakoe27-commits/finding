"""Context rollout must preserve the shared model and refuse stale or foreign changes."""
import json
import os
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


class ContextFixture:
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


class ContextDeploymentTests(ContextFixture, unittest.TestCase):
    def test_inspection_reads_shape_and_names_without_installing_or_exposing_secrets(self):
        script = self.root / 'start-model.sh'
        script.write_text('#!/bin/bash\nexec /srv/python -m vllm --api-key private-script-key --max-model-len 32768\n')
        self.fragment.write_text('[Service]\nEnvironment="API_KEY=private-env-key" "MODEL_CONTEXT=32768"\n'
            'EnvironmentFile=-/etc/private-model.env\nExecStart=/bin/bash -lc "' + str(script) + ' --api-key=private-cli-key"\n')
        result = deploy.inspect('vllm.service', self.call)
        self.assertEqual(result['state'], 'inspection_only')
        self.assertEqual(result['files'][0]['environment_names'], ['API_KEY', 'MODEL_CONTEXT'])
        self.assertEqual(result['files'][0]['environment_files'], ['-/etc/private-model.env'])
        self.assertEqual(result['files'][0]['starts'][0]['program'], '/bin/bash')
        self.assertEqual(result['launch_scripts'][0]['shape']['context_arguments'], [{'kind': 'literal', 'tokens': 32768}])
        for secret in ('private-script-key', 'private-env-key', 'private-cli-key'):
            self.assertNotIn(secret, json.dumps(result))
        self.assertFalse(self.state.exists())
        self.assertFalse(self.destination.exists())
        self.assertTrue(all(command[:2] == ['systemctl', 'show'] for command in self.calls))

    def test_inspection_finds_context_inside_a_shell_command_and_masks_other_values(self):
        shape = deploy.command_shape('/bin/bash -lc \'exec /srv/python -m vllm --max-model-len ${MODEL_CONTEXT} --api-key "private-inner-key"\'')
        self.assertEqual(shape['shell_command']['context_arguments'], [{'kind': 'variable', 'name': 'MODEL_CONTEXT'}])
        self.assertEqual(shape['variable_names'], ['MODEL_CONTEXT'])
        self.assertNotIn('private-inner-key', json.dumps(shape))

    def test_inspection_never_reads_script_paths_passed_as_credentials(self):
        credential = self.root / 'private-credential.sh'
        credential.write_text('this is confidential, not a launch script')
        self.fragment.write_text('[Service]\nExecStart=/srv/python -m vllm --api-key ' + str(credential) + ' --max-model-len 32768\n')
        result = deploy.inspect('vllm.service', self.call)
        self.assertEqual(result['launch_scripts'], [])
        self.assertNotIn(str(credential), json.dumps(result))
        self.assertNotIn('confidential', json.dumps(result))

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


class LaunchScriptDeploymentTests(ContextFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.script = self.root / 'start_vllm38.sh'
        # Match the real 1Cat launcher shape: setup first, then a continued exec.
        self.source = (b'#!/bin/bash\nset -euo pipefail\nexport HF_HOME="$HOME/.cache/huggingface"\n'
            b'exec /srv/1Cat/bin/python -m vllm.entrypoints.openai.api_server \\\n'
            b'  --model mattbucci/Qwen3.8-27B-AWQ --api-key private-script-key \\\n'
            b'  --served-model-name QuantTrio/Qwen3.5-35B-A3B-AWQ --max-model-len 32768 \\\n'
            b'  --gpu-memory-utilization 0.82 --tensor-parallel-size 2 --max-num-seqs 4 --port 8000\n')
        self.script.write_bytes(self.source); self.script.chmod(0o750)
        self.unit_source = ('[Service]\nUser=model\nEnvironment="CUDA_VISIBLE_DEVICES=0,1"\n'
            'ExecStart=' + str(self.script) + '\n').encode()
        self.fragment.write_bytes(self.unit_source)

    def script_plan(self):
        return deploy.prepare('vllm.service', 131072, self.call, launch_script=self.script)

    def test_launcher_prepare_only_changes_the_context_in_private_candidate(self):
        result = self.script_plan(); path = Path(result['plan'])
        self.assertEqual(result['change_kind'], 'launch_script')
        self.assertEqual(self.script.read_bytes(), self.source)
        self.assertEqual(self.fragment.read_bytes(), self.unit_source)
        self.assertFalse(self.destination.exists())
        self.assertEqual((path.parent / 'original.sh').read_bytes(), self.source)
        self.assertEqual((path.parent / 'candidate.sh').read_bytes(), self.source.replace(b'32768', b'131072'))
        self.assertEqual((path.parent / 'original.sh').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('private-script-key', json.dumps(result))
        self.assertTrue(any('-n' in command and 'bash' in command for command in self.calls))
        self.assertFalse(any('restart' in command or 'daemon-reload' in command for command in self.calls))

    def test_launcher_apply_and_rollback_preserve_owner_mode_unit_and_other_arguments(self):
        original_metadata = deploy.script_metadata(self.script)
        plan = Path(self.script_plan()['plan'])
        result = deploy.apply(plan, self.call)
        self.assertEqual(result['state'], 'script_updated')
        self.assertTrue(result['restart_required'])
        self.assertEqual(self.script.read_bytes(), self.source.replace(b'32768', b'131072'))
        self.assertEqual(deploy.script_metadata(self.script), original_metadata)
        self.assertEqual(self.fragment.read_bytes(), self.unit_source)
        self.assertFalse(self.destination.exists())
        self.assertEqual(deploy.rollback(plan, self.call)['state'], 'script_restored')
        self.assertEqual(self.script.read_bytes(), self.source)
        self.assertEqual(deploy.script_metadata(self.script), original_metadata)
        self.assertEqual(deploy.rollback(plan, self.call)['state'], 'script_already_restored')
        self.assertFalse(any('restart' in c or 'daemon-reload' in c for c in self.calls))

    def test_launcher_manual_edits_block_apply_and_rollback(self):
        plan = Path(self.script_plan()['plan'])
        self.script.write_bytes(self.source + b'\n# manual edit\n')
        with self.assertRaises(ValueError): deploy.apply(plan, self.call)
        self.assertTrue(self.script.read_bytes().endswith(b'# manual edit\n'))
        self.script.write_bytes(self.source)
        deploy.apply(plan, self.call)
        self.script.write_bytes(self.script.read_bytes() + b'\n# another edit\n')
        with self.assertRaises(ValueError): deploy.rollback(plan, self.call)
        self.assertTrue(self.script.read_bytes().endswith(b'# another edit\n'))

    def test_launcher_unit_changes_or_wrong_script_block_preparation_or_apply(self):
        other = self.root / 'other-project.sh'; other.write_bytes(self.source)
        with self.assertRaises(ValueError): deploy.prepare('vllm.service', 131072, self.call, launch_script=other)
        self.assertFalse(self.state.exists())
        plan = Path(self.script_plan()['plan'])
        self.fragment.write_bytes(self.unit_source + b'\n# changed unit\n')
        with self.assertRaises(ValueError): deploy.apply(plan, self.call)
        self.assertEqual(self.script.read_bytes(), self.source)

    def test_launcher_mode_or_candidate_changes_do_not_overwrite_the_script(self):
        plan = Path(self.script_plan()['plan'])
        self.script.chmod(0o700)
        with self.assertRaises(ValueError): deploy.apply(plan, self.call)
        self.script.chmod(0o750)
        (plan.parent / 'candidate.sh').write_bytes(b'#!/bin/bash\necho unexpected\n')
        with self.assertRaises(ValueError): deploy.apply(plan, self.call)
        self.assertEqual(self.script.read_bytes(), self.source)

    def test_launcher_failed_atomic_replace_keeps_original_script(self):
        plan = Path(self.script_plan()['plan'])
        with patch.object(deploy.os, 'replace', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError): deploy.apply(plan, self.call)
        self.assertEqual(self.script.read_bytes(), self.source)
        self.assertEqual(list(self.script.parent.glob('.matchlab-context-*')), [])

    def test_real_bash_validation_does_not_execute_launcher_or_shell_startup(self):
        marker = self.root / 'launcher-was-executed'
        startup_marker = self.root / 'startup-was-executed'
        startup = self.root / 'unsafe-bash-env.sh'
        startup.write_text('touch ' + str(startup_marker) + '\n')
        self.script.write_bytes(self.source.replace(b'set -euo pipefail', ('touch ' + str(marker) + '\nset -euo pipefail').encode()))
        def validate(args):
            return deploy.run(args) if args[0] == 'env' else self.call(args)
        with patch.dict(os.environ, {'BASH_ENV': str(startup), 'ENV': str(startup)}):
            deploy.prepare('vllm.service', 131072, validate, launch_script=self.script)
        self.assertFalse(marker.exists())
        self.assertFalse(startup_marker.exists())

    def test_launcher_keeps_extended_attributes_during_update_and_restore(self):
        try:
            os.setxattr(self.script, 'user.matchlab_fixture', b'preserved')
        except (AttributeError, OSError):
            self.skipTest('Filesystem does not support user extended attributes')
        plan = Path(self.script_plan()['plan'])
        deploy.apply(plan, self.call)
        self.assertEqual(os.getxattr(self.script, 'user.matchlab_fixture'), b'preserved')
        deploy.rollback(plan, self.call)
        self.assertEqual(os.getxattr(self.script, 'user.matchlab_fixture'), b'preserved')


if __name__ == '__main__':
    unittest.main()
