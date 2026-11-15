#!/usr/bin/env bash
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root'; exit 1; }
model_source=${1:?Usage: install-server.sh /path/to/model-directory}
script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
for file in model.cbm feature-schema.json metrics.json; do
    test -f "$model_source/$file"
done
apt-get update
apt-get install -y python3-venv
id prognozich-model >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin prognozich-model
id prognozich-model-link >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/prognozich-model-link --shell /usr/sbin/nologin prognozich-model-link
install -d -m 755 /opt/prognozich-model /opt/prognozich-model/model
install -m 644 "$script_dir/server.py" /opt/prognozich-model/server.py
python3 -m venv /opt/prognozich-model/venv
/opt/prognozich-model/venv/bin/pip install -r "$script_dir/requirements.txt"
for file in model.cbm feature-schema.json metrics.json; do
    install -m 644 "$model_source/$file" "/opt/prognozich-model/model/$file"
done
/opt/prognozich-model/venv/bin/python /opt/prognozich-model/server.py --model-dir /opt/prognozich-model/model --check
install -d -m 750 -o root -g prognozich-model /etc/prognozich-model
if [[ ! -f /etc/prognozich-model/token ]]; then
    (umask 077; python3 -c 'import secrets; print(secrets.token_hex(32))' > /etc/prognozich-model/token)
fi
chown root:prognozich-model /etc/prognozich-model/token
chmod 640 /etc/prognozich-model/token
cat > /etc/systemd/system/prognozich-model.service <<'UNIT'
[Unit]
Description=Prognozich experimental CatBoost CPU inference
After=network.target

[Service]
User=prognozich-model
Group=prognozich-model
Environment=MODEL_TOKEN_FILE=/etc/prognozich-model/token
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=OMP_NUM_THREADS=2
ExecStart=/opt/prognozich-model/venv/bin/python /opt/prognozich-model/server.py --model-dir /opt/prognozich-model/model
Restart=on-failure
RestartSec=5
MemoryMax=2G
CPUQuota=200%
TasksMax=128
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
RestrictAddressFamilies=AF_INET AF_UNIX
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable prognozich-model.service
systemctl restart prognozich-model.service
echo 'Installed. Check: curl --fail http://127.0.0.1:8090/health'
