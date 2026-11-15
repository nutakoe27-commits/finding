# CPU inference on VPS-2

The server and tunnel installers provide isolated inference infrastructure.
The optional worker below connects it to the admin interface. The collector
database, payments, current Qwen answers and public probabilities remain unchanged.

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

## Verified bundle and feature preparation

`all-20261115-1`: 1030 labelled events / 2060 participant rows; 54 features;
111 trees. CatBoost 1.2.10 reproduced training log loss
0.5456160873478885 and Brier 0.1851532990332195 in the isolated test environment.
No holdout evaluation exists. `check-client.py` is a transport/compatibility
test on an old dataset row, **not** a new-match quality measurement.

Expected feature-preparation hash from metrics:
`7953db0396a7ef577c525645f0b9dc421148891f97ffc71e69911558c2140b5c`.
The exact preparation source has been located in
`nutakoe27-commits/doscbot`, branch `feat/fresh-sports-collector`, commits
`1d12898`, `2732619`, and `e64e355` (identical preparation hash).
This revision hashes SEVEN modules: `training`, `training_evidence`,
`training_diagnostics`, `profiles`, `store`, `outcomes`, `monitor_data`.
The original VPS collector archive was older; computing the old five-module
hash on the trainer is also incorrect and does not indicate corrupted code.
Use an isolated copy of the verified source, not an update of the live collector.
`audit-features.py --code /path/to/source-root` checks this exact hash and
reconstructs all exported participant features from immutable samples in the
new collector database opened with `mode=ro` and `query_only=ON`. It does not
query outcomes or run inference, and prints only counts and differing field
names. A successful report is `all_exported_features_match`. Matching this
dataset is a compatibility check, not an out-of-sample model evaluation.
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

## Prospective admin experiment (after the successful feature audit)

The site now has an **«Обученная модель»** admin tab. It reads a small report;
model inference is performed by a separate timer on VPS-1 through the existing
SSH tunnel. Current Qwen answers, public results and billing are unchanged.
The report endpoint requires an authenticated admin, is not cached, and does
not return raw features or credentials.

On **VPS-1**, after `all_exported_features_match`:

```bash
cd /root/matchlab
git pull --ff-only origin main
bash deploy/participant_model/install-worker.sh
systemctl restart matchlab.service
journalctl -u prognozich-model-worker.service -n 5 --no-pager -o cat
```

Stop if any command fails. The installer starts the worker and its timer, and
adds only `PARTICIPANT_MODEL_REPORT` to a dedicated `matchlab.service` drop-in.
It does not change the site database or the collector. It uses system Python
stdlib on VPS-1; CatBoost remains in its isolated environment on VPS-2.

Storage and resource boundaries:

- Collector `/var/lib/doscbot-collector/data/dataset.sqlite3`: SQLite `mode=ro`,
  `query_only=ON`; systemd additionally mounts the database file read-only.
  Its directory is writable so SQLite can create WAL/SHM coordination files,
  including when a checkpointed WAL database has no sidecars yet. The reader
  does not modify table contents or checkpoint the source. Individual sidecars
  are not bind-mounted because the collector may recreate them. Do not use
  `immutable=1`: it would ignore the collector's new WAL commits.
- Exact feature preparation is vendored from doscbot revision
  `e64e355ad7a76ad2f04a666fad6b82c9c2f45080` (see `preparation-source.json`).
  All vendored files are checked against `vendor/SHA256.json` before use.
- Model and original training export remain in
  `/var/lib/doscbot-models/all-20261115-1`. The worker checks their hashes and
  training population. The verified version has **1,030 labelled training
  events / 2,060 rows**; the full export has 1,696 events including unlabelled ones.
- Worker code: `/opt/prognozich-model-client/app`. Credentials remain root-only;
  the site never receives the inference token.
- Immutable prediction files: `/var/lib/prognozich-model-client/frozen/` (private).
  Back up this directory: losing it loses the prospective evidence.
- Admin summary: `/var/lib/prognozich-model-client/report.json` (atomic replacement).
- `prognozich-model-worker.timer`: a run 60 seconds after the previous run ends.
  At most 10 new events per run, two sequential CPU inferences each. Worker cap
  50% CPU / 1 GiB RAM. Does not use Qwen slots, GPU, or the old finding database.

The original evaluation modes require snapshots after model readiness. In
`primary_llm.v1`, policy `recent_statistics.v1` also admits validated pre-match
statistics captured before training, at most 24 hours old at inference. The
forecast must still be issued after model readiness. Training events and
aliases with the same statistics ID are excluded. Schedule freshness comes from
the latest snapshot in this collector's own `source/winline.sqlite3`, whose path
must match the dataset's `source_archive` ownership metadata. The event must be
present in that snapshot, with a matching `last_seen` and raw-content checksum.
Change history alone is not a freshness clock: unchanged events retain their
old history timestamp despite being observed again. Only when that source archive
is absent can a native observation less than five minutes old be used instead.
A stale/untrusted catalogue or a missing event cannot fall back to old history.
The source database is also opened read-only and its file is protected by systemd;
SQLite can create its WAL/SHM sidecars as described above. The saved prediction
includes schedule provenance (snapshot/time/content hash), separate from features.
Unchanged participants/start time, a supported market, a schedule observation
within five minutes through inference completion, and at least the training
horizon (60 minutes) remaining are required.
An already observed outcome blocks prediction. Network calls occur after the
collector read transaction closes. Partial pairs and late completions are not
saved. One frozen pair per event and model version; subsequent runs only score it.

Scoring uses the pinned preparation code's conservative result matching and
normal-period rules. Unsupported overtime/retirements, ambiguous identities,
changed start times or missing outcomes remain unscored with a reason. This can
bias coverage, so the UI exposes pending counts and diagnostics. It shows Brier,
log loss, a same-sample odds baseline, calibration bins and per-sport metrics.
Two participant rows from one match are correlated; event count is the sample
size. Probabilities are not renormalised into a two-way/three-way distribution.
Training fit is not presented as future accuracy; no threshold automatically
marks the model as validated. Compare versions only on appropriately held-out
new fixtures, not by retraining on these outcomes and reusing the same test.

No records immediately after installation is normal if fresh eligible snapshots
are missing; check the admin diagnostics. A worker error prints its type and,
for SQLite failures, its numeric/symbolic code in the journal (no SQL, tokens or
payloads); the service exits unsuccessfully instead of reporting success.
A report older than five minutes is visibly
marked stale. To pause collection while preserving data:

```bash
systemctl disable --now prognozich-model-worker.timer
systemctl stop prognozich-model-worker.service
```

Targeted verification:

```bash
PYTHONPATH=tests .venv/bin/python -m unittest test_participant_model_worker test_participant_model_admin
PYTHONPATH=tests .venv/bin/python tests/browser_participant_model.py
```

## Statistics-only modes A and B

The worker accepts a separate `statistics_only.v1` model trained by doscbot.
The exact 30-feature allowlist is pinned in `vendor/collector/statistics_contract.py`.
Its hash must match the trained model. All odds, implied prices, margin, market
counts and their missing indicators are excluded from inference. Rows without
verified form/season counts for both participants are skipped.

A chooses a participant before prices are examined; B uses the identical frozen
probability and applies a fixed paper-only price gate (p >= .55, p*odds-1 >= .05).
Both decisions and the policy are saved with the forecast. Model/policy versions
use separate directories. The old model is explicitly labelled as using prices.

Admin rows expose frozen snapshot prices and the A/B decisions. Aggregate A
accuracy excludes missing preferences; B shows coverage, settled accuracy,
pending bets and unit-stake paper ROI. Skips are not wins. Training accuracy
is not future accuracy. Public Qwen answers and billing are not switched.

For scoring only, the worker reconstructs the original full legacy features
from the frozen source sample to reuse its evidence/timing validator. These
features never go to CatBoost. The frozen statistics-only input and decisions
are checked independently before settlement.

Training and two-server deployment are documented in doscbot:
[Statistics models](https://github.com/nutakoe27-commits/doscbot/blob/feat/fresh-sports-collector/docs/statistics-models.md).
`activate-statistics-bundle.sh` verifies bundle hashes and the remote model
identity before updating the private `worker.env`.

## Основная ИИ после статистической модели

Новый режим `primary_llm.v1`: одна модель без коэффициентов и отдельное решение
настроенной основной ИИ в свободном системном потоке сайта. Пороговый фильтр B
не используется; старые эксперименты сохраняют свой режим. Запрос фиксируется
до матча, токены и причины доступны только в админке.

[Свежий экспорт и полный порядок развёртывания](https://github.com/nutakoe27-commits/doscbot/blob/feat/fresh-sports-collector/docs/primary-model-review.md).

### Расширение охвата без переобучения

В `primary_llm.v1` цены проверяются отдельно от снимка статистики. Требуется
подробный `market_document` не старше 5 минут с проверенной контрольной суммой,
совпадающими участниками, спортом, временем и областью расчёта исхода; либо цены
из только что собранного проверенного снимка. Просроченные/закрытые/неоднозначные
рынки не заменяются догадками. При отсутствии свежего подтверждения причина —
`fresh_market_missing_or_changed`.

Исходный снимок, признаки и рынок остаются неизменными для проверки результата.
Обновлённые коэффициенты сохраняются отдельно в `price_quote`, с идентификатором
наблюдения и временем. Основная ИИ получает именно их. Админка показывает
`sample_as_of` для статистики и `prices_as_of` для цен. Уже записанные прогнозы
прежнего режима сохраняются; новая политика указана в `input_policy`.

Обработчик публикует `collection-priority.json` (0644, без личных данных) рядом
с `report.json`: поддерживаемые спорты, идентификаторы обучающих/записанных матчей,
время и хеш модели. Обновлённый doscbot использует его для 80% мест очереди на
матчи через 2–24 часа, сохраняя 20% для общего обхода. При устаревшем файле
(15 минут), отсутствии или ошибке действует прежний порядок.

Для обновления на VPS-1: обновить репозиторий сайта, остановить таймер и службу
`prognozich-model-worker`, выполнить `install-worker.sh` (он сохраняет текущий
`worker.env`), перезапустить `matchlab.service`. Затем обновить репозиторий
сборщика и перезапустить `doscbot-collector.service`. Модель/окружение VPS-2,
обучающие данные, параметры CatBoost и семь модулей подготовки признаков не меняются.
