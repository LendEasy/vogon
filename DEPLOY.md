# Deploying vogon

## Requirements

- Python 3.10 to 3.12 (3.13 works with the `audioop-lts` wheel, pulled in automatically).
- For phone calls: the SpeexDSP echo canceller, built from source at install
  time — `apt-get install git g++ swig libspeex-dev libspeexdsp-dev` first, then
  `deploy/build_speexdsp.sh <python>` (the binding does not build as a pip
  extra, so the script runs the two steps that work). Without it `/health`
  reports `aec: false` and the gateway refuses to start under the default
  `GATEWAY_CAPABILITY_POLICY=refuse`; set `warn` to run browser-only.
- A Deepgram API key. Twilio credentials and a public HTTPS URL for phone calls.
- Redis when you run more than one gateway process or want calls to survive a
  restart (`GATEWAY_SESSION_STORE=redis`, `REDIS_URL`). The default in-memory
  store is fine for one process.

## systemd

`deploy/vogon.service` is a template: a dedicated user, a venv under
`/opt/vogon`, secrets in `/etc/vogon/vogon.env` (root-owned, mode 0600), writes
confined to `STORAGE_BASE_PATH`. Install with:

```bash
sudo useradd --system --home /opt/vogon vogon
sudo -u vogon python3 -m venv /opt/vogon/.venv
sudo -u vogon /opt/vogon/.venv/bin/pip install "setuptools<81" "le-vogon[front] @ git+https://github.com/LendEasy/vogon"
sudo -u vogon deploy/build_speexdsp.sh /opt/vogon/.venv/bin/python
sudo install -m 0600 env.example /etc/vogon/vogon.env     # then edit
sudo install -m 0644 deploy/vogon.service /etc/systemd/system/vogon.service
sudo systemctl daemon-reload && sudo systemctl enable --now vogon
curl -s localhost:8002/health
```

## Docker

```bash
docker build -t vogon -f voice-gateway/Dockerfile .      # from a checkout holding both packages
docker run --env-file .env -p 8002:8002 -v vogon-data:/data vogon
```

The image builds the echo canceller in a first stage and runs as an
unprivileged user. Mount `/data` for recordings and metrics.

## Twilio

Point nothing at Twilio by hand: the gateway places outbound calls through the
REST API and gives each a media-stream URL under `GATEWAY_PUBLIC_BASE_URL`.
That URL must be reachable from Twilio over HTTPS; the status callback lands on
`/v1/twilio/status`. Inbound numbers are configured on the Twilio side to
stream to `wss://<your host>/ws/twilio`.

## Storage

Recordings, the audio event log and the frames log are written under
`RECORDINGS_PATH`; metrics under `STORAGE_BASE_PATH/metrics`. With
`S3_ENABLED=true` and `S3_BUCKET`, artifacts are uploaded at call end. The
timeline viewer and the raw artifacts are served per tenant at
`/v1/tenants/{tenant}/calls/{call_id}/…`, authenticated by the service key.

## Observability

Logs are one line per event with the call id in every line. Metrics are written
as OpenTelemetry JSON under the `METRICS_NAMESPACE` prefix (`vogon.` by
default); point a collector at the files or replace the sink. `/health` lists
the native capabilities so a deploy can assert them.

## Operating notes

- One gateway process handles many calls; CPU is spent in the echo canceller
  and the μ-law codec. Scale horizontally behind a sticky load balancer keyed
  on the call token; the session store must then be Redis.
- Set `MAX_CALL_DURATION_SEC`; the silence ladder and the duration cap are the
  two ways a call ends without anyone hanging up.
- Changing any value in `docs/voice-timing-constants.md` means changing the
  document too; the test that pins them fails otherwise, on purpose.
