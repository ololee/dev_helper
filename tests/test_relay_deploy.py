import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('deploy_relay', SOURCE / 'scripts/deploy_relay.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class RelayDeployTests(unittest.TestCase):
    def test_archive_contains_public_allowlist_and_no_runtime_files(self):
        with tarfile.open(fileobj=io.BytesIO(deploy.archive()), mode='r:gz') as archive:
            names = set(archive.getnames())
            self.assertEqual(names, set(deploy.FILES) | {'devhelper-relay.service', 'devhelper-relay-renew.service', 'devhelper-relay-renew.timer', 'renew-certificate.sh'})
            self.assertTrue(all(not name.startswith(('/', '../', 'data/', 'models/')) for name in names))
            for item in archive.getmembers():
                self.assertTrue(item.isfile())
                self.assertEqual(item.uid, 0)
                self.assertEqual(item.uname, '')

    def test_rejects_shell_destination_and_invalid_port(self):
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / 'identity.pem'
            key.write_text('SYNTHETIC KEY NEVER UPLOADED')
            for host, port in [('root@host;echo unsafe', 22), ('-oProxyCommand@host', 22), ('root@host', 0)]:
                with self.assertRaises(ValueError):
                    deploy.ssh_args(host, key, port)

    def test_installer_script_parses_and_staging_is_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / 'identity.pem'
            key.write_text('SYNTHETIC KEY NEVER UPLOADED')
            for staging in (True, False):
                calls = []
                args = ['deploy_relay.py', '--ssh-host', 'root@192.0.2.1', '--identity-file', str(key), '--public-ip', '192.0.2.1', '--agree-tos']
                if staging:
                    args.append('--staging')
                with patch.object(sys, 'argv', args), patch.object(deploy.subprocess, 'run', side_effect=lambda *a, **k: calls.append((a, k))), patch('builtins.print'):
                    deploy.main()
                self.assertEqual(len(calls), 2)
                script = calls[-1][1]['input']
                subprocess.run(['sh', '-n'], input=script, check=True)
                self.assertNotIn(b'SYNTHETIC KEY', calls[0][1]['input'])
                if staging:
                    self.assertIn(b'/etc/devhelper-relay/acme-staging', script)
                    self.assertNotIn(b'systemctl enable', script)
                else:
                    self.assertIn(b'systemctl enable --now devhelper-relay.service', script)

    def test_certificate_hot_reload_keeps_service_running(self):
        self.assertIn('--signal=USR1', deploy.HOOK)
        self.assertNotIn('restart', deploy.HOOK)
        self.assertIn('User=devhelper-relay', deploy.SERVICE)
        self.assertIn('00,12:00:00', deploy.RENEW_TIMER)

    def test_http_install_keeps_data_and_does_not_request_certificates(self):
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / 'identity.pem'
            key.write_text('SYNTHETIC KEY NEVER UPLOADED')
            calls = []
            args = ['deploy_relay.py', '--ssh-host', 'root@192.0.2.1',
                    '--identity-file', str(key), '--public-ip', '192.0.2.1', '--http-port', '8081']
            with patch.object(sys, 'argv', args), patch.object(deploy.subprocess, 'run', side_effect=lambda *a, **k: calls.append((a, k))), patch('builtins.print'):
                deploy.main()
            self.assertEqual(len(calls), 3)
            self.assertIn('s.bind', calls[0][0][0][-1])
            with tarfile.open(fileobj=io.BytesIO(calls[1][1]['input']), mode='r:gz') as archive:
                service = archive.extractfile('devhelper-relay.service').read().decode()
            self.assertIn('--port 8081 --data-dir /var/lib/devhelper-relay', service)
            self.assertNotIn('--ssl-certfile', service)
            script = calls[-1][1]['input']
            subprocess.run(['sh', '-n'], input=script, check=True)
            self.assertIn(b'devhelper-relay.service.before-http', script)
            self.assertIn(b'disable --now devhelper-relay-renew.timer', script)
            self.assertNotIn(b'certbot', script)
            self.assertNotIn(b'rm ', script)

    def test_http_port_conflict_aborts_before_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / 'identity.pem'
            key.write_text('SYNTHETIC KEY NEVER UPLOADED')
            args = ['deploy_relay.py', '--ssh-host', 'root@192.0.2.1',
                    '--identity-file', str(key), '--public-ip', '192.0.2.1', '--http-port', '8080']
            with patch.object(sys, 'argv', args), patch.object(deploy.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'port check')) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    deploy.main()
            self.assertEqual(run.call_count, 1)
