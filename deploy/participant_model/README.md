# CPU inference on VPS-2

This installs only inference infrastructure. It does not modify the website,
its database, the collector database, payments, current Qwen answers, or public
probabilities. The first intended consumer is the admin interface.

VPS-2: `89.111.184.88`, SSH 22, Ubuntu 26.04/Python 3.14. VPS-1:
`89.111.143.45`. The server binds **only** `127.0.0.1:8090`; VPS-1 reaches it
at `127.0.0.1:18090` through a dedicated SSH key. `/predict` additionally
requires a private bearer token. No firewall opening for HTTP is necessary.
Verify the SSH server fingerprint when first connecting; the tunnel uses
strict host-key checking and never accepts new keys automatically.

## Installation (commands on VPS-1 as root)

```bash
cd /root/matchlab
git pull --ff-only origin main
ssh root@89.111.184.88 'mkdir -p /root/prognozich-model-input'
scp -r deploy/participant_model root@89.111.184.88:/root/
scp /var/lib/doscbot-models/all-20261115-1/model/{model.cbm,metrics.json,feature-schema.json} root@89.111.184.88:/root/prognozich-model-input/
ssh root@89.111.184.88 'bash /root/participant_model/install-server.sh /root/prognozich-model-input'
bash deploy/participant_model/install-tunnel.sh
curl --fail http://127.0.0.1:18090/health
python3 deploy/participant_model/check-client.py
```

Stop on any failed command. The installers may ask for the VPS-2 root SSH
password; never paste passwords, private keys or bearer tokens into chat.
Neither installer modifies SSH daemon configuration or restarts the website.
Server resources: 2 CPU-equivalents, 2 GiB memory cap, 2 simultaneous inference
calls. CatBoost uses 2 threads per call. Model and code are read-only for the
service user. The endpoint does not read any database.

VPS-1 token: `/etc/prognozich-model-client/token` (root-only). Future application
integration must use an explicitly scoped credential for its service user;
do not make this file world-readable. The SSH account has no interactive
command access; its local forwarding destination is restricted to port 8090.

## Contract

POST `/predict`: JSON with exactly `model_sha256` and `features`.
`features` must contain exactly the schema columns, including all missing
indicators. Categories are strings. Missing numeric values are JSON null,
converted to NaN, never zero. Feature order is categorical, numeric, then
missing indicators. Outcome/target fields are rejected, not sent to CatBoost.
The selected probability is class 1. Participant probabilities are **not**
renormalized, and a draw probability is not inferred by subtraction.

Responses include the model/schema/features SHA256, sport, selection side,
market scope, and raw probability. This API accepts already prepared features;
it does not establish their provenance or chronology. The website's adapter
must enforce sample binding, pre-match cutoff and the original training
feature preparation before calling it. It must persist pre-match predictions
separately for prospective evaluation; a later replay is not a live forecast.

## Verified bundle and outstanding integration dependency

`all-20261115-1`: 1030 labelled events / 2060 participant rows; 54 features;
111 trees. CatBoost 1.2.10 reproduced training log loss
0.5456160873478885 and Brier 0.1851532990332195 in the isolated test environment.
No holdout evaluation exists. `check-client.py` is a transport/compatibility
test on an old dataset row, **not** a new-match quality measurement.

Expected feature-preparation hash from metrics:
`7953db0396a7ef577c525645f0b9dc421148891f97ffc71e69911558c2140b5c`.
Supplied collector code hash:
`607b6ef4456e957b83d07e3a0674092df44b295e0556a1674d8f32d2fa70e677`.
Resolve the source revision difference before calculating new match features.
Training sample minimum lead was 60 minutes. The original classifier has
normal-period label restrictions; do not reinterpret it as an overtime or
retirement settlement model. Existing public Qwen results are separate.

## Diagnostics / stop

VPS-2: `systemctl status prognozich-model.service` and
`journalctl -u prognozich-model.service -n 20 --no-pager`.
VPS-1: `systemctl status prognozich-model-tunnel.service` and
`journalctl -u prognozich-model-tunnel.service -n 20 --no-pager`.

To disable only this addition, run `systemctl disable --now
prognozich-model-tunnel.service` on VPS-1 and `systemctl disable --now
prognozich-model.service` on VPS-2. Source datasets and the website are untouched.

Targeted verification:
```bash
PARTICIPANT_MODEL_BUNDLE=/path/to/bundle /path/to/catboost-venv/bin/python -m unittest discover -s tests -p test_participant_model_server.py
```
