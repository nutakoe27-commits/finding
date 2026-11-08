from pathlib import Path
import tempfile
import unittest

from scripts.configure_domain import MARKER,apply_site,check_paths,configuration,conflicts,domain_name


class DomainDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name)
        (self.root/'available').mkdir(); (self.root/'enabled').mkdir()
        self.available=self.root/'available/matchlab-prognozich.online.conf'
        self.enabled=self.root/'enabled'/self.available.name
        self.other=self.root/'available/other-project'
        self.other.write_bytes(b'server { listen 80 default_server; server_name existing.example; }\n')
        (self.enabled.parent/'other-project').symlink_to(self.other)
        self.original=self.other.read_bytes()

    def assert_other_unchanged(self):
        self.assertEqual(self.other.read_bytes(),self.original)
        self.assertEqual((self.enabled.parent/'other-project').resolve(),self.other)

    def test_rejects_domain_injection_and_normalizes_hostname(self):
        self.assertEqual(domain_name('PROGNOZICH.ONLINE.'),'prognozich.online')
        for value in ('http://prognozich.online','x.online; listen 443','x.online\nssl','*.online','x.online/path'):
            with self.assertRaises(ValueError): domain_name(value)

    def test_finds_existing_domain_in_another_active_config(self):
        dump=f'''# configuration file {self.enabled}:
server_name prognozich.online;
# configuration file /etc/nginx/sites-enabled/provider:
server {{ server_name other.example
    prognozich.online; }} # comment
'''
        self.assertEqual(conflicts(dump,'prognozich.online',self.enabled),['/etc/nginx/sites-enabled/provider'])

    def test_success_adds_only_own_site_and_uses_reload(self):
        calls=[]
        apply_site(self.available,self.enabled,configuration('prognozich.online',8080),run=calls.append)
        self.assertEqual(self.enabled.resolve(),self.available)
        self.assertEqual(calls,[['nginx','-t'],['systemctl','reload','nginx.service']])
        self.assert_other_unchanged()

    def test_never_overwrites_unmanaged_file_or_another_projects_link(self):
        self.available.write_text('another application')
        with self.assertRaises(ValueError): check_paths(self.available,self.enabled)
        self.available.unlink(); self.enabled.symlink_to(self.other)
        with self.assertRaises(ValueError): check_paths(self.available,self.enabled)
        self.assert_other_unchanged()

    def test_bad_nginx_configuration_restores_existing_own_site(self):
        previous=(MARKER+'\n# previous working configuration\n').encode()
        self.available.write_bytes(previous); self.enabled.symlink_to(self.available)
        calls=[]
        def runner(command):
            calls.append(command)
            if len(calls)==1: raise RuntimeError('syntax error')
        with self.assertRaises(RuntimeError): apply_site(self.available,self.enabled,'broken',run=runner)
        self.assertEqual(self.available.read_bytes(),previous)
        self.assertEqual(self.enabled.resolve(),self.available)
        self.assertEqual(sum('reload' in call for call in calls),1)
        self.assert_other_unchanged()

    def test_failed_reload_removes_only_new_files(self):
        calls=[]
        def runner(command):
            calls.append(command)
            if len(calls)==2: raise RuntimeError('reload failed')
        with self.assertRaises(RuntimeError): apply_site(self.available,self.enabled,MARKER,run=runner)
        self.assertFalse(self.available.exists()); self.assertFalse(self.enabled.is_symlink())
        self.assert_other_unchanged()

    def test_failed_host_probe_rolls_back_even_after_reload(self):
        def failed(): raise ValueError('another virtual host answered')
        calls=[]
        with self.assertRaises(RuntimeError): apply_site(self.available,self.enabled,MARKER,run=calls.append,verify=failed)
        self.assertFalse(self.enabled.is_symlink())
        self.assertEqual(sum('reload' in call for call in calls),2)
        self.assert_other_unchanged()


if __name__=='__main__': unittest.main()
