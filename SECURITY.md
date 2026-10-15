# Security

## Reporting

Report vulnerabilities privately to security@lendeasy.com (or through GitHub's
private vulnerability reporting on this repository). Do not open a public issue
for a vulnerability. We acknowledge reports within three business days.

## What the gateway handles

- **Audio and transcripts of real phone calls.** Recordings and the audio
  event log are written to `RECORDINGS_PATH` and optionally uploaded to S3.
  Treat that storage as sensitive personal data: restrict the directory to the
  service user, encrypt the bucket, set retention. The turn service decides
  whether a call is recorded (`CallConfig.recording`); the gateway honours it.
- **Provider credentials** (Deepgram, Twilio, OpenRouter, OpenAI, AWS) from the
  environment or `.env`. The gateway never logs them. Keep `.env` out of
  version control and root-owned on servers.
- **The service key.** Every management route (`/v1/tenants/{tenant}/…`) requires
  `Authorization: Bearer $GATEWAY_SERVICE_KEY`. Auth is on whenever that
  variable is set and off when it is empty; there is no separate switch. With
  it empty, anyone who can reach the port can create calls (and so place
  outbound dials billed to your Twilio account), read any call's recording
  by its id, and post call-status updates, so leave it empty only on a
  gateway that nothing but the developer's own machine can reach. The key is
  one per calling service,
  not per tenant: the `{tenant}` in the path is data supplied by the
  authenticated application, and it scopes what a call can read, not who may
  ask. The gateway presents the same key on its status relay back to the
  application. The browser WebSocket is authenticated by the per-call session
  token minted at creation; the Twilio media WebSocket carries no token
  (Twilio strips query parameters) and is admitted by resolving its CallSid
  against the session store.
- **The turn service's key.** On every `POST /turn/v1/*` the gateway presents
  a bearer: the `auth_token` the application bound to the call at creation,
  or `TURN_SERVICE_API_KEY` from the environment when the call carries none.
  The gateway sends it and never checks it; validating it is the turn
  service's job. The per-call token is held in the session record for the
  life of the call.
- **Twilio callbacks.** Every Twilio webhook that changes state (the call-status
  webhook, the escalation callbacks) is checked against `X-Twilio-Signature`
  with Twilio's request validator when `TWILIO_AUTH_TOKEN` is set. Twilio signs
  the public URL, so `GATEWAY_PUBLIC_BASE_URL` must be the URL Twilio actually
  calls when the gateway sits behind a proxy.
- **Tenant isolation.** Artifact routes check that the call belongs to the
  tenant in the URL; a token for one tenant cannot read another tenant's
  recording.

## What the gateway does not do

It does not interpret the content of the call beyond three narrow questions
about the *line* (is the caller interrupting, is this a voicemail greeting,
which approved filler fits), and it never generates speech of its own: every
word it plays was either returned by the turn service or drawn from a pool the
turn service approved.
