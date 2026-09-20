# BD Coach

Unified repository for **BD Coach**, a self-hostable AI sales-operations assistant.
This repo merges what used to be two separate repos (`bd-coach-config` and
`bd-coach-infra`) into one product with two halves:

| Path | Was | Contents |
|---|---|---|
| [`config/`](config/) | `bd-coach-config` | Prompts, personas, DLP rules, topic intents, adaptive-card templates (JSON/YAML), and the DLP test harness. Version-controlled agent **behaviour**. |
| [`infra/`](infra/) | `bd-coach-infra` | The self-hostable Docker Compose **stack** (LibreChat, LiteLLM, Ollama, Rasa, n8n, Mattermost, Keycloak, Baserow, Nextcloud, MinIO, Qdrant, Postgres, Vault, observability, Traefik), deploy scripts, and docs. |

The infra stack mounts the config tree read-only at runtime (e.g. LiteLLM reads
`config/dlp/`, Rasa reads `config/topics/`, LibreChat and n8n read `config/`),
so the two halves are designed to live together.

## Layout

```
bd-coach/
├── config/      Agent behaviour (prompts, personas, DLP, topics, cards, knowledge)
│   ├── prompts/     Master system prompt (locked) + live-coach nudge prompt
│   ├── personas/    CEO / USA_BD / EU_BD scopes → Keycloak groups
│   ├── topics/      Rasa intents + LibreChat tool mapping
│   ├── dlp/         HR/compensation regex rules + run_tests.py
│   ├── cards/       Adaptive-card JSON templates
│   └── knowledge/   Connector manifest
└── infra/       Self-hostable stack
    ├── compose/     docker-compose.yml (+ gpu / slim / hostinger / coach overlays)
    ├── litellm/     Model gateway config + DLP/audit hooks
    ├── coach/       Live call coaching service (optional overlay)
    ├── librechat/   Agent UI config
    ├── keycloak/    OIDC realm export
    ├── postgres/    DB init
    ├── observability/  Prometheus config
    ├── docs/        HOSTINGER.md and friends
    └── scripts/     bootstrap.sh, install-hostinger.sh
```

## Quick start

The stack runs from `infra/` and reads behaviour from `config/`.

```bash
cd infra
cp .env.example .env          # set BD_COACH_DOMAIN, passwords, BASEROW_* ids, GROQ_API_KEY
docker compose -f compose/docker-compose.yml --env-file .env up -d
chmod +x scripts/bootstrap.sh && ./scripts/bootstrap.sh
```

`bootstrap.sh` pulls Ollama models, creates the MinIO audit bucket, prints
manual setup steps, and runs the config DLP tests (`config/dlp/run_tests.py`).

Validate the behaviour half on its own at any time:

```bash
python config/dlp/run_tests.py
```

### Hostinger VPS

See **[infra/docs/HOSTINGER.md](infra/docs/HOSTINGER.md)** for the full deploy.
Clone this repo to `/opt/bd-coach`; the stack then lives at `/opt/bd-coach/infra`.

## Optional: live call coaching

BD Coach coaches *around* calls — weekly templates, stale-deal sweeps, gate
watchdogs, pipeline digests. This optional overlay adds coaching *during* the
call: a browser call surface, live transcription, and short nudges when the
conversation goes sideways.

```bash
cd infra
docker compose -f compose/docker-compose.yml \
               -f compose/docker-compose.coach.yml up -d
```

**The trade, stated plainly:** call audio is carried by [Agora](https://console.agora.io/),
a hosted SFU, so a third party is on the path of every conversation you coach.
WebRTC needs a relay and a relay is somebody's server — there is no version of
this feature without that cost.

What stays in-house is everything that is *not* forced out:

- **Transcription** runs on the stack's own `whisper` container, not Agora's
  hosted STT. Calls are relayed by a third party, not processed by one.
- **The decision to interrupt** is deterministic arithmetic over the transcript
  (talk ratio, monologue length, discovery questions, objection and competitor
  mentions, missing next step) — not a model call every few seconds.
- **Transcripts are redacted before reaching the model gateway**, using the same
  `config/dlp/` rules the outbound hook enforces, read in the other direction.
  That matters because LiteLLM has a Groq fallback: redacting at the source
  makes the failover path safe by construction instead of by hoping Ollama stays
  up.
- **A model outage degrades, it doesn't break** — the nudge falls back to the
  cue engine's own wording.

Without `AGORA_APP_ID` / `AGORA_APP_CERTIFICATE` the overlay is inert and the
rest of the stack is unaffected.

Full boundary analysis, cue table, and setup: **[infra/docs/LIVE-COACH.md](infra/docs/LIVE-COACH.md)**.

## Per-half docs

- [`config/README.md`](config/README.md) — behaviour layout, CI checks, persona setup, resilience parameters.
- [`infra/README.md`](infra/README.md) — full service table, first boot, DNS records, DR notes.

## CI

`.github/workflows/ci.yml` runs two jobs:

- **config** — JSON/YAML parse checks, yamllint, and the DLP regex tests.
- **infra** — Python syntax check on hooks/scripts, yamllint, the live-coach cue
  and redaction tests (`infra/coach/run_tests.py`), and `docker compose config -q`
  for both the base stack and the live-coach overlay.

### Row 34 — guardrail fixtures: DONOR (the pattern's origin — organic, CI-enforced)

bd-coach is the donor the canonical row-34 fixture pattern extends. The full pattern already lives here and runs in CI on every PR (`ci.yml:42` → `python config/dlp/run_tests.py`):

- **Fixtures as data:** `config/dlp/test_fixtures.yaml` pins prompt-shaped expectations in two named lists — `must_block_for_non_ceo` (peer compensation figures, salary-review language, termination/gate clauses) and `must_pass_for_non_ceo` (legitimate coaching requests: weekly reports, point thresholds, follow-up emails, MTD summaries) — so additions are YAML lines, not test code.
- **Policy separate from fixtures:** the regexes live in `config/dlp/restricted_hr_comp.yaml` under `rules.block_for_non_ceo`; fixtures assert against the policy, never copy it — a policy change that breaks an expectation fails CI rather than silently passing.
- **Fail-closed runner:** `config/dlp/run_tests.py` loads both YAML files, compiles the block patterns, prints every failing sample, and exits non-zero on any mismatch between expectation and outcome (`sys.exit(main())`).
- **Sibling surface:** `infra/coach/run_tests.py` applies the same shape to the live-coach cue and redaction tests (CI job `infra`, `ci.yml:77`).

This is the origin the wave-C row-34 adopter assessments were patterned from (cognitrader-bsc #6, self-improving-outreach #3/#4): the same must-block/must-pass fixture shape, policy-separated rules, and a fail-closed CI runner.
