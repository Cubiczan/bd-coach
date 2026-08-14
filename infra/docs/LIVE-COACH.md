# Live call coaching

**Optional overlay. Off unless you deploy it.**

BD Coach coaches around calls — it pushes weekly templates, sweeps stale deals,
watches gates, and digests the pipeline. What it never did is help during the
one moment that decides the deal: the call itself.

This overlay adds that. It also makes the one trade BD Coach otherwise avoids,
so read the boundary section before deploying it.

## The boundary

BD Coach's whole posture is self-hostable, no-SaaS, with DLP enforced at the
model gateway. Live coaching needs three things — a call to listen to, speech
turned into text, and a model to phrase the advice — and only the first of them
genuinely requires a third party.

So the split is deliberate:

| Stage | Where it runs | Leaves your infra? |
|---|---|---|
| Call audio transport | Agora (hosted SFU) | **Yes** |
| Speech-to-text | `whisper` container, already in the stack | No |
| When to interrupt | `app/cues.py`, plain arithmetic | No |
| Redaction | `app/redaction.py`, shared DLP rules | No |
| Phrasing the nudge | LiteLLM → Ollama | No, unless Groq failover fires |

**What you are accepting:** Agora relays the call audio, so a third party is on
the path of every conversation you coach. That is the cost of the feature and
there is no version of it that avoids the cost — WebRTC needs a relay, and a
relay is somebody's server.

**What you are not accepting:** transcripts are produced on your own hardware.
The obvious implementation would use Agora's Real-Time Transcription, which is
less code and lower latency, but it would mean every sales conversation is also
*processed* by a third party rather than merely relayed. The stack already runs
`faster-whisper-server`, so it does not have to.

### The Groq caveat

`infra/litellm/config.yaml` configures a Groq fallback for when Ollama is
unavailable. On a normal day the transcript never leaves the box. On the day
Ollama is down, it does.

Rather than hoping the primary stays up, transcripts are redacted **before**
they reach LiteLLM, using the same `config/dlp/restricted_hr_comp.yaml` rules
the outbound hook enforces — read in the other direction. Anything the DLP hook
would block on the way out is scrubbed on the way in, plus long digit runs
(card and account numbers read aloud on a call).

If the DLP mount is missing, the service logs at ERROR and redacts nothing
rather than refusing to start — a broken mount should not take a sales team's
calls offline. Watch for that log line.

## Why a cue engine instead of streaming everything to a model

The naive build pipes every transcript chunk to an LLM and shows whatever comes
back. That fails twice: it burns a model call every few seconds on a box that
is also serving the rest of BD Coach, and it buries the seller in advice during
a live call, which is worse than silence.

So the decision to interrupt is made in `app/cues.py`, deterministically:

| Cue | Fires when | Priority |
|---|---|---|
| `talk_ratio` | seller has ≥70% of airtime over the last 90s | 100 |
| `objection` | prospect raised a concern in the last 45s | 95 |
| `monologue` | seller spoke ≥75s without a break | 90 |
| `no_next_step` | ≥80% through the expected duration, no next step proposed | 88 |
| `competitor` | a configured competitor was named | 85 |
| `early_pricing` | pricing came up with <3 discovery questions asked | 80 |
| `no_discovery` | ≥4 minutes in with <2 questions asked | 75 |

Only the highest-priority cue is shown, and cooldowns (45s global, 240s per
cue) mean the panel stays quiet most of the call. The model is only asked to
phrase a nudge once a cue has already fired — and if the model is unavailable,
the cue's own wording is shown instead, so an Ollama outage degrades coaching
quality rather than removing it.

This also makes coaching behaviour unit-testable without a model in the loop,
matching how the rest of BD Coach scores pipeline. `infra/coach/run_tests.py`
covers it with stdlib + pyyaml only, and runs in CI.

Talk-ratio and monologue are measured over a trailing window with utterance
durations **clipped to that window** — a four-minute monologue that ended just
inside the window must not contribute four minutes of airtime to a ninety-second
measure, or the cue keeps firing long after the seller handed the call back.

## Deploying

```bash
# infra/.env
AGORA_APP_ID=<from console.agora.io>
AGORA_APP_CERTIFICATE=<enable the certificate on the project first>
COACH_COMPETITORS=Gong,Chorus,Clari      # optional
COACH_EXPECTED_DURATION=1800             # seconds, drives the next-step cue
```

```bash
cd infra
docker compose \
  -f compose/docker-compose.yml \
  -f compose/docker-compose.coach.yml up -d
```

Then open `https://coach.<BD_COACH_DOMAIN>`, enter a call id, and join. The
prospect joins the same call id.

Without `AGORA_APP_ID` / `AGORA_APP_CERTIFICATE` the service still starts and
reports `call_surface: false` on `/healthz`; the rest of the stack is unaffected.

## Layout

| Path | Role |
|---|---|
| `infra/coach/app/cues.py` | The cue engine. Pure, deterministic, tested. |
| `infra/coach/app/redaction.py` | Inbound DLP redaction over the shared rule file. |
| `infra/coach/app/transcribe.py` | Client for the in-house Whisper container. |
| `infra/coach/app/coaching.py` | Phrases a fired cue via LiteLLM; degrades to cue text. |
| `infra/coach/app/main.py` | FastAPI: `/token`, `/ws/coach`, static call surface. |
| `infra/coach/vendor/agora/` | Official Agora AccessToken2 builder (MIT), vendored. |
| `config/prompts/live_coach.v1.0.md` | The nudge prompt, versioned like the others. |

The token builder is vendored rather than pip-installed because the community
PyPI package (`agora-token-builder`) only emits the legacy 006 token format.
These are the official files from `AgoraIO/Tools`, stdlib-only. Re-vendor from
upstream rather than editing them.

## Audio chunking note

`MediaRecorder` emits a decodable container only in its first blob; later blobs
from the same recorder are headerless fragments Whisper cannot open. The client
therefore starts a fresh recorder every 4 seconds, so each chunk is a complete,
independently decodable file. If you change `CHUNK_SECONDS` in
`static/coach.js`, keep that property.

## Not done

- Nudges are not persisted. End-of-call metrics are returned over the socket
  and shown in the panel, but nothing is written to Baserow yet.
- `MM_HOOK_COACH` is read from config but no Mattermost post is sent.
- Speaker attribution relies on Agora's per-user tracks, so it is exact for a
  two-party call and untested with three or more participants.
