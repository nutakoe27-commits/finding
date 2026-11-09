"""Changing a profile must preserve the launcher and never manage its service."""
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from scripts import set_qwen_profile as profiles


SOURCE = b'''#!/bin/bash
set -euo pipefail
export HF_HOME="$HOME/.cache/huggingface"
exec /srv/1Cat/bin/python -m vllm.entrypoints.openai.api_server \\
  --model mattbucci/Qwen3.8-27B-AWQ --served-model-name legacy-alias \\
  --api-key private-fixture-key --dtype float16 --quantization awq \\
  --max-model-len 32768 --gpu-memory-utilization 0.82 \\
  --max-num-batched-tokens 8192 --max-num-seqs 4 --tensor-parallel-size 2 --port 8000
'''


class ProfileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.script = self.root / 'start_vllm38.sh'
        self.script.write_bytes(SOURCE); self.script.chmod(0o750)
        change = patch.object(profiles, 'BACKUPS', self.root); change.start(); self.addCleanup(change.stop)

    def test_trial_profile_preserves_unrelated_options_and_has_private_backup(self):
        metadata = profiles.files.script_metadata(self.script)
        with patch.object(profiles.files, 'run', wraps=profiles.files.run) as run:
            result = profiles.configure(self.script, '128k')
        text = self.script.read_text(); argv = profiles.tokens(text)
        for flag, value in profiles.PROFILES['128k'].items():
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertEqual(argv.count('--enable-chunked-prefill'), 1)
        self.assertEqual(argv.count('--enforce-eager'), 1)
        for marker in ('private-fixture-key', 'legacy-alias', 'mattbucci/Qwen3.8-27B-AWQ', '$HOME/.cache/huggingface'):
            self.assertIn(marker, text)
        self.assertNotIn('private-fixture-key', json.dumps(result))
        self.assertEqual(Path(result['backup']).read_bytes(), SOURCE)
        self.assertEqual(Path(result['backup']).stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(result['backup']).parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(profiles.files.script_metadata(self.script), metadata)
        self.assertFalse(result['restart_performed'])
        self.assertTrue(all(call.args[0][:3] == ['env', '-u', 'BASH_ENV'] for call in run.call_args_list))

    def test_profiles_are_idempotent_and_working_rollback_restores_numeric_settings(self):
        profiles.configure(self.script, '128k')
        before = self.script.read_bytes()
        self.assertEqual(profiles.configure(self.script, '128k')['state'], 'already_configured')
        self.assertEqual(self.script.read_bytes(), before)
        profiles.configure(self.script, '32k')
        argv = profiles.tokens(self.script.read_text())
        self.assertNotIn('--enforce-eager', argv)
        for flag, value in profiles.PROFILES['32k'].items():
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertEqual(profiles.configure(self.script, '32k')['state'], 'already_configured')

    def test_explicit_cache_limit_and_ambiguous_options_are_not_overwritten(self):
        for extra in (' --kv-cache-memory-bytes 123456', ' --max-num-seqs 8'):
            self.script.write_bytes(SOURCE.rstrip() + extra.encode() + b'\n')
            original = self.script.read_bytes()
            with self.assertRaises(ValueError): profiles.configure(self.script, '128k')
            self.assertEqual(self.script.read_bytes(), original)

    def test_equal_syntax_quoted_values_and_existing_flags_are_supported(self):
        text = SOURCE.decode().replace('--gpu-memory-utilization 0.82', '--gpu-memory-utilization="0.82"')
        text = text.replace('--max-model-len 32768', '--enable-chunked-prefill=false --enforce-eager false --max-model-len=32768')
        self.script.write_text(text)
        profiles.configure(self.script, '128k')
        argv = profiles.tokens(self.script.read_text())
        self.assertIn('--gpu-memory-utilization=0.60', argv)
        self.assertEqual(argv.count('--enable-chunked-prefill'), 1)
        self.assertEqual(argv.count('--enforce-eager'), 1)
        self.assertNotIn('false', argv)

    def test_syntax_error_or_intervening_edit_does_not_install_candidate(self):
        self.script.write_bytes(SOURCE + b'if broken syntax\n')
        original = self.script.read_bytes()
        with self.assertRaises(ValueError): profiles.configure(self.script, '128k')
        self.assertEqual(self.script.read_bytes(), original)
        self.script.write_bytes(SOURCE)
        def edit_during_validation(args):
            self.script.write_bytes(SOURCE + b'# another administrator edited this\n')
            return ''
        with patch.object(profiles.files, 'run', side_effect=edit_during_validation):
            with self.assertRaises(ValueError): profiles.configure(self.script, '128k')
        self.assertTrue(self.script.read_bytes().endswith(b'# another administrator edited this\n'))


if __name__ == '__main__':
    unittest.main()
