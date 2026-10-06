#!/usr/bin/env python3
"""Upload only relay source via SSH; no clone, account data, or key copying."""
from __future__ import annotations
import argparse
import io
import ipaddress
from pathlib import Path
import re
import shlex
import subprocess
import tarfile

SOURCE = Path(__file__).resolve().parents[1]
FILES = ('relay/__init__.py', 'relay/server.py', 'relay/requirements.txt', 'relay/PROTOCOL.md')

SERVICE = '''[Unit]
Description=DevHelper HTTP relay
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=devhelper-relay
Group=devhelper-relay
WorkingDirectory=/opt/devhelper-relay
ExecStart=/opt/devhelper-relay/.venv/bin/python -m relay.server --host 0.0.0.0 --port 443 --data-dir /var/lib/devhelper-relay --ssl-certfile /etc/devhelper-relay/fullchain.pem --ssl-keyfile /etc/devhelper-relay/privkey.pem
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=5
TimeoutStopSec=35
StateDirectory=devhelper-relay
StateDirectoryMode=0700
UMask=0077
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/devhelper-relay

[Install]
WantedBy=multi-user.target
'''
RENEW_SERVICE = '''[Unit]
Description=Renew DevHelper relay IP certificate

[Service]
Type=oneshot
ExecStart=/opt/devhelper-relay/.venv/bin/certbot renew --quiet --config-dir /etc/devhelper-relay/acme --work-dir /var/lib/devhelper-relay-acme --logs-dir /var/log/devhelper-relay-acme --deploy-hook /opt/devhelper-relay/renew-certificate.sh
'''
RENEW_TIMER = '''[Unit]
Description=Check DevHelper relay certificate twice daily

[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
'''
HOOK = '''#!/bin/sh
set -eu
install -o root -g devhelper-relay -m 0640 "$RENEWED_LINEAGE/fullchain.pem" /etc/devhelper-relay/fullchain.pem
install -o root -g devhelper-relay -m 0640 "$RENEWED_LINEAGE/privkey.pem" /etc/devhelper-relay/privkey.pem
if systemctl is-active --quiet devhelper-relay.service; then
  systemctl kill --kill-who=main --signal=USR1 devhelper-relay.service
fi
'''


def ssh_args(host, key, port):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9_.:-]*', host):
        raise ValueError('Use an explicit user@host SSH destination')
    if not 1 <= port <= 65535:
        raise ValueError('Invalid SSH port')
    key = key.expanduser().resolve()
    if not key.is_file():
        raise ValueError('SSH identity file not found')
    return ['ssh', '-i', str(key), '-p', str(port), '-o', 'BatchMode=yes', '-o',
            'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=10', host]


def archive():
    data = io.BytesIO()
    def public_metadata(item):
        item.uid = item.gid = item.mtime = 0
        item.uname = item.gname = ''
        return item
    with tarfile.open(fileobj=data, mode='w:gz') as out:
        for relative in FILES:
            path = SOURCE / relative
            if not path.is_file() or path.is_symlink():
                raise ValueError('Missing public relay source: ' + relative)
            out.add(path, arcname=relative, recursive=False, filter=public_metadata)
        extras = {'devhelper-relay.service': SERVICE, 'devhelper-relay-renew.service': RENEW_SERVICE,
                  'devhelper-relay-renew.timer': RENEW_TIMER, 'renew-certificate.sh': HOOK}
        for name, content in extras.items():
            raw = content.encode()
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(raw), 0o755 if name.endswith('.sh') else 0o644
            out.addfile(item, io.BytesIO(raw))
    return data.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-host', required=True, help='Explicit SSH user@host')
    parser.add_argument('--identity-file', required=True, type=Path)
    parser.add_argument('--ssh-port', type=int, default=22)
    parser.add_argument('--public-ip', required=True)
    parser.add_argument('--agree-tos', action='store_true', help="Accept Let's Encrypt subscriber terms for certificate issuance")
    parser.add_argument('--staging', action='store_true', help='Test ACME issuance without enabling the HTTPS service')
    parser.add_argument('--update-only', action='store_true', help='Upload source and restart an already configured service')
    args = parser.parse_args()
    public_ip = str(ipaddress.ip_address(args.public_ip))
    ssh = ssh_args(args.ssh_host, args.identity_file, args.ssh_port)
    if not args.update_only and not args.agree_tos:
        parser.error('Certificate installation requires --agree-tos')
    subprocess.run(ssh + ['mkdir -p /opt/devhelper-relay && tar -xzf - -C /opt/devhelper-relay'], input=archive(), check=True)
    if args.update_only:
        subprocess.run(ssh + ['systemctl restart devhelper-relay.service && systemctl is-active devhelper-relay.service'], check=True)
        return
    install = '''set -eu
command -v python3.11 >/dev/null || {{ echo 'Install Python 3.11 using your Linux package manager first'; exit 1; }}
getent passwd devhelper-relay >/dev/null || useradd --system --home-dir /var/lib/devhelper-relay --shell /sbin/nologin devhelper-relay
python3.11 -m venv /opt/devhelper-relay/.venv
/opt/devhelper-relay/.venv/bin/python -m pip install --disable-pip-version-check --upgrade pip
/opt/devhelper-relay/.venv/bin/python -m pip install --disable-pip-version-check -r /opt/devhelper-relay/relay/requirements.txt 'certbot>=5.4,<6'
install -d -o root -g devhelper-relay -m 0750 /etc/devhelper-relay
chmod 0755 /opt/devhelper-relay/renew-certificate.sh
/opt/devhelper-relay/.venv/bin/certbot certonly --standalone --non-interactive --agree-tos --register-unsafely-without-email --preferred-profile shortlived --ip-address {ip} --config-dir /etc/devhelper-relay/{config} --work-dir /var/lib/devhelper-relay-acme --logs-dir /var/log/devhelper-relay-acme {staging}
'''.format(ip=shlex.quote(public_ip), staging='--staging' if args.staging else '', config='acme-staging' if args.staging else 'acme')
    if not args.staging:
        install += '''RENEWED_LINEAGE=/etc/devhelper-relay/acme/live/{ip} /opt/devhelper-relay/renew-certificate.sh
install -m 0644 /opt/devhelper-relay/devhelper-relay.service /etc/systemd/system/devhelper-relay.service
install -m 0644 /opt/devhelper-relay/devhelper-relay-renew.service /etc/systemd/system/devhelper-relay-renew.service
install -m 0644 /opt/devhelper-relay/devhelper-relay-renew.timer /etc/systemd/system/devhelper-relay-renew.timer
systemctl daemon-reload
systemctl enable --now devhelper-relay.service devhelper-relay-renew.timer
systemctl is-active devhelper-relay.service
'''.format(ip=shlex.quote(public_ip))
    subprocess.run(ssh + ['sh -s'], input=install.encode(), check=True)
    print('ACME staging verified' if args.staging else 'Relay installed; verify HTTPS health and pair both devices with one connection code')


if __name__ == '__main__':
    main()
