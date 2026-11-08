from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ssl
import os
import subprocess
import tempfile
from threading import Thread
import unittest

from scripts.configure_domain import MARKER,apply_site,check_paths,configuration,conflicts,domain_name,validate_tls,probe_https,prevent_tls_downgrade,prevent_acme_downgrade,probe_acme,validate_acme_root


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

    def test_tls_configuration_is_scoped_to_matchlab_and_keeps_worker_protection(self):
        config=configuration('prognozich.online',8080,'/etc/ssl/site/fullchain.pem','/etc/ssl/site/privkey.pem')
        self.assertIn('listen 443 ssl;',config)
        self.assertIn('listen [::]:443 ssl;',config)
        self.assertIn('ssl_certificate /etc/ssl/site/fullchain.pem;',config)
        self.assertIn('ssl_certificate_key /etc/ssl/site/privkey.pem;',config)
        self.assertIn('server_name prognozich.online;',config)
        self.assertIn('refresh/(claim|finish)',config)
        self.assertNotIn('default_server',config)
        calls=[]
        apply_site(self.available,self.enabled,config,run=calls.append)
        self.assert_other_unchanged()

    def test_tls_rejects_wrong_domain_wrong_key_and_unsafe_paths(self):
        cert=self.root/'cert.pem'; key=self.root/'key.pem'; other=self.root/'other.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2','-subj','/CN=prognozich.online',
                        '-addext','subjectAltName=DNS:prognozich.online','-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(other)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        validate_tls('prognozich.online',str(cert),str(key))
        for domain,certificate,private in (('other.example',str(cert),str(key)),('prognozich.online',str(cert),str(other)),('prognozich.online',str(cert)+';inject',str(key))):
            with self.assertRaises(ValueError): validate_tls(domain,certificate,private)

    def test_tls_probe_checks_certificate_hostname_and_site_response(self):
        cert=self.root/'cert.pem'; key=self.root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2','-subj','/CN=prognozich.online',
                        '-addext','subjectAltName=DNS:prognozich.online','-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                self.send_response(200)
                self.send_header('X-Matchlab-Site','prognozich.online')
                self.end_headers(); self.wfile.write(b'{"events":[]}')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(cert,key)
        server.socket=tls.wrap_socket(server.socket,server_side=True)
        worker=Thread(target=server.serve_forever,daemon=True); worker.start()
        try:
            context=ssl.create_default_context(cafile=str(cert))
            probe_https('prognozich.online',port=server.server_port,context=context)
            with self.assertRaises(ssl.SSLCertVerificationError): probe_https('other.example',port=server.server_port,context=context)
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=5)

    def test_existing_tls_cannot_be_removed_by_an_http_only_update(self):
        self.available.write_text(configuration('prognozich.online',8080,'/etc/ssl/site/cert.pem','/etc/ssl/site/key.pem'))
        with self.assertRaises(ValueError): prevent_tls_downgrade(self.available,None)
        prevent_tls_downgrade(self.available,'/etc/ssl/site/cert.pem')
        self.assert_other_unchanged()

    def test_acme_location_and_renewal_cannot_be_removed_silently(self):
        config=configuration('prognozich.online',8080,acme_root='/var/lib/matchlab-acme')
        self.assertIn('location ^~ /.well-known/acme-challenge/',config)
        self.assertIn('root /var/lib/matchlab-acme;',config)
        self.assertIn('refresh/(claim|finish)',config)
        self.available.write_text(config)
        with self.assertRaises(ValueError): prevent_acme_downgrade(self.available,None)
        prevent_acme_downgrade(self.available,'/var/lib/matchlab-acme')
        self.assert_other_unchanged()

    def test_acme_probe_serves_and_removes_only_its_test_file(self):
        folder=self.root/'acme'/'.well-known'/'acme-challenge'; folder.mkdir(parents=True)
        marker=folder/'existing-file'; marker.write_text('unchanged')
        root=folder.parent.parent
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                file=folder/self.path.rsplit('/',1)[-1]
                self.send_response(200); self.send_header('X-Matchlab-Site','prognozich.online')
                self.end_headers(); self.wfile.write(file.read_bytes())
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=Thread(target=server.serve_forever,daemon=True); worker.start()
        try:
            validate_acme_root(str(root))
            probe_acme('prognozich.online',str(root),port=server.server_port)
            self.assertEqual([item.name for item in folder.iterdir()],['existing-file'])
            self.assertEqual(marker.read_text(),'unchanged')
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=5)

    def test_renewal_hook_reloads_only_for_our_certificate(self):
        root=Path(__file__).resolve().parent.parent
        nginx=self.root/'nginx'; systemctl=self.root/'systemctl'; log=self.root/'calls'
        nginx.write_text('#!/bin/sh\nprintf "nginx %s\\n" "$*" >> "$MATCHLAB_TEST_LOG"\n')
        systemctl.write_text('#!/bin/sh\nprintf "systemctl %s\\n" "$*" >> "$MATCHLAB_TEST_LOG"\n')
        nginx.chmod(0o755); systemctl.chmod(0o755)
        hook=(root/'deploy/matchlab-certbot-renewal.sh').read_text().replace('/usr/sbin/nginx',str(nginx)).replace('/usr/bin/systemctl',str(systemctl))
        fixture=self.root/'hook.sh'; fixture.write_text(hook)
        env={**os.environ,'MATCHLAB_TEST_LOG':str(log),'RENEWED_LINEAGE':'/etc/letsencrypt/live/another.example'}
        subprocess.run(['sh',str(fixture)],check=True,env=env)
        self.assertFalse(log.exists())
        env['RENEWED_LINEAGE']='/etc/letsencrypt/live/prognozich.online'
        subprocess.run(['sh',str(fixture)],check=True,env=env)
        self.assertEqual(log.read_text().splitlines(),['nginx -t','systemctl reload nginx.service'])
        log.unlink(); nginx.write_text('#!/bin/sh\nprintf "nginx %s\\n" "$*" >> "$MATCHLAB_TEST_LOG"\nexit 1\n')
        self.assertNotEqual(subprocess.run(['sh',str(fixture)],env=env).returncode,0)
        self.assertEqual(log.read_text().splitlines(),['nginx -t'])

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
