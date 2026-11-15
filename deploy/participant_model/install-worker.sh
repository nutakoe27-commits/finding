#!/usr/bin/env bash
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run as root on VPS-1.'; exit 1; }
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
test -f /var/lib/doscbot-collector/data/dataset.sqlite3
test -f /var/lib/doscbot-models/all-20261115-1/model/metrics.json
test -s /etc/prognozich-model-client/token
install -d -m 755 /opt/prognozich-model-client/app /var/lib/prognozich-model-client
install -d -m 700 /var/lib/prognozich-model-client/frozen
install -m 644 "$source_dir/worker.py" /opt/prognozich-model-client/app/worker.py
cp -R "$source_dir/vendor" /opt/prognozich-model-client/app/
python3 - <<'PY'
import importlib.util
p='/opt/prognozich-model-client/app/worker.py'
s=importlib.util.spec_from_file_location('worker',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
m.load_source()
print('Pinned feature preparation: OK')
PY
cat > /etc/systemd/system/prognozich-model-worker.service <<'UNIT'
[Unit]
Description=Freeze and evaluate prospective participant model probabilities (admin only)
After=network-online.target prognozich-model-tunnel.service
Wants=network-online.target prognozich-model-tunnel.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/prognozich-model-client/app/worker.py
WorkingDirectory=/opt/prognozich-model-client/app
User=root
UMask=0077
TimeoutStartSec=360
CPUQuota=50%
MemoryMax=1G
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadOnlyPaths=/var/lib/doscbot-collector/data /var/lib/doscbot-models
ReadWritePaths=/var/lib/prognozich-model-client
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
IPAddressDeny=any
IPAddressAllow=localhost
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/prognozich-model-worker.timer <<'UNIT'
[Unit]
Description=Check new participant model inputs and outcomes every minute
[Timer]
OnBootSec=45
OnUnitInactiveSec=60
Unit=prognozich-model-worker.service
[Install]
WantedBy=timers.target
UNIT
install -d -m 755 /etc/systemd/system/matchlab.service.d
cat > /etc/systemd/system/matchlab.service.d/participant-model.conf <<'UNIT'
[Service]
Environment=PARTICIPANT_MODEL_REPORT=/var/lib/prognozich-model-client/report.json
UNIT
systemctl daemon-reload
systemctl enable --now prognozich-model-worker.timer
systemctl start prognozich-model-worker.service
echo 'Worker installed. Restart matchlab.service after updating the site code to show the admin tab.'
