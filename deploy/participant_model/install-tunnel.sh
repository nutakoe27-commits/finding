#!/usr/bin/env bash
# Run on VPS-1 after a trusted root SSH connection to VPS-2 is configured.
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root'; exit 1; }
host=89.111.184.88
home_dir=/var/lib/prognozich-model-tunnel
test -f /root/.ssh/known_hosts
ssh-keygen -F "$host" -f /root/.ssh/known_hosts >/dev/null || {
    echo 'First connect to root@89.111.184.88 and verify its host fingerprint.'; exit 1;
}
id prognozich-model-tunnel >/dev/null 2>&1 || useradd --system --create-home --home-dir "$home_dir" --shell /usr/sbin/nologin prognozich-model-tunnel
install -d -m 700 -o prognozich-model-tunnel -g prognozich-model-tunnel "$home_dir/.ssh"
if [[ ! -f "$home_dir/.ssh/id_ed25519" ]]; then
    ssh-keygen -q -t ed25519 -N '' -C prognozich-model-tunnel -f "$home_dir/.ssh/id_ed25519"
fi
ssh-keygen -F "$host" -f /root/.ssh/known_hosts > "$home_dir/.ssh/known_hosts"
chmod 600 "$home_dir/.ssh/"*
chown -R prognozich-model-tunnel:prognozich-model-tunnel "$home_dir/.ssh"
public_key=$(cat "$home_dir/.ssh/id_ed25519.pub")
printf 'restrict,port-forwarding,permitopen="127.0.0.1:8090",command="/usr/sbin/nologin" %s\n' "$public_key" |
    ssh -o StrictHostKeyChecking=yes root@"$host" '
        set -eu
        id prognozich-model-link >/dev/null
        keydir=/var/lib/prognozich-model-link/.ssh
        install -d -m 700 -o prognozich-model-link -g prognozich-model-link "$keydir"
        touch "$keydir/authorized_keys"
        IFS= read -r keyline
        grep -qxF "$keyline" "$keydir/authorized_keys" || printf "%s\n" "$keyline" >> "$keydir/authorized_keys"
        chmod 600 "$keydir/authorized_keys"
        chown prognozich-model-link:prognozich-model-link "$keydir/authorized_keys"
    '
install -d -m 700 /etc/prognozich-model-client
token_tmp=$(mktemp /etc/prognozich-model-client/token.XXXXXX)
trap 'rm -f "$token_tmp"' EXIT
scp -o StrictHostKeyChecking=yes root@"$host":/etc/prognozich-model/token "$token_tmp"
install -m 600 "$token_tmp" /etc/prognozich-model-client/token
cat > /etc/systemd/system/prognozich-model-tunnel.service <<'UNIT'
[Unit]
Description=Private SSH tunnel to Prognozich CPU inference
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
User=prognozich-model-tunnel
ExecStart=/usr/bin/ssh -F /dev/null -N -T -p 22 -i /var/lib/prognozich-model-tunnel/.ssh/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/var/lib/prognozich-model-tunnel/.ssh/known_hosts -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ConnectTimeout=10 -L 127.0.0.1:18090:127.0.0.1:8090 prognozich-model-link@89.111.184.88
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable prognozich-model-tunnel.service
systemctl restart prognozich-model-tunnel.service
echo 'Installed. Check: curl --fail http://127.0.0.1:18090/health'
