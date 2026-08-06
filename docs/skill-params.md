# Aegis Skill Platform Parameters

Aegis automatically injects these environment variables to every skill process. Skills should **not** ask users to configure these — they are provided by the platform.

## Platform Parameters (auto-injected)

| Env Var | Type | Description |
|---------|------|-------------|
| `AEGIS_GATEWAY_URL` | `string` | LLM gateway endpoint (e.g. `http://localhost:5407`). Proxies to whatever LLM provider the user has configured (OpenAI, Anthropic, local). Skills should use this for all LLM calls — it handles auth, routing, and model selection. |
| `AEGIS_VLM_URL` | `string` | Local VLM (Vision Language Model) server endpoint (e.g. `http://localhost:5405`). Available when the user has a local VLM running. |
| `AEGIS_SKILL_ID` | `string` | The skill's unique identifier (e.g. `home-security-benchmark`). |
| `AEGIS_SKILL_PARAMS` | `JSON string` | User-configured parameters from `config.yaml` (see below). |
| `AEGIS_PORTS` | `JSON string` | All Aegis service ports as JSON. Use the URL vars above instead of parsing this directly. |

## User Parameters (from config.yaml)

Skills can define user-configurable parameters in a `config.yaml` file alongside `SKILL.md`. Aegis parses this at install time and renders a config panel in the UI. User values are passed as JSON via `AEGIS_SKILL_PARAMS`.

### config.yaml Format

```yaml
params:
  - key: mode
    label: Test Mode
    type: select
    options: [option1, option2, option3]
    default: option1
    description: "Human-readable description shown in the config panel"

  - key: verbose
    label: Verbose Output
    type: boolean
    default: false
    description: "Enable detailed logging"

  - key: threshold
    label: Confidence Threshold
    type: number
    default: 0.7
    description: "Minimum confidence score (0.0–1.0)"

  - key: apiEndpoint
    label: Custom API Endpoint
    type: string
    default: ""
    description: "Optional override for external API"
```

Supported types: `string`, `boolean`, `select`, `number`

### Reading config.yaml in Your Skill

```javascript
// Node.js — parse AEGIS_SKILL_PARAMS
let skillParams = {};
try { skillParams = JSON.parse(process.env.AEGIS_SKILL_PARAMS || '{}'); } catch {}

const mode = skillParams.mode || 'default';
const verbose = skillParams.verbose || false;
```

```python
# Python — parse AEGIS_SKILL_PARAMS
import os, json

skill_params = json.loads(os.environ.get('AEGIS_SKILL_PARAMS', '{}'))
mode = skill_params.get('mode', 'default')
verbose = skill_params.get('verbose', False)
```

### Precedence

```
CLI flags > AEGIS_SKILL_PARAMS > Platform env vars > Defaults
```

When a skill supports both CLI arguments and `AEGIS_SKILL_PARAMS`, CLI flags should take priority. Platform-injected env vars (like `AEGIS_GATEWAY_URL`) are always available regardless of `config.yaml`.

## Gateway as Proxy

The gateway (`AEGIS_GATEWAY_URL`) is an OpenAI-compatible proxy. Skills call it like any OpenAI endpoint — the gateway handles:

- **API key management** — user configures keys in Aegis settings
- **Provider routing** — OpenAI, Anthropic, local models
- **Model selection** — user picks model in Aegis UI

Skills should **not** need raw API keys. If a skill needs direct provider access in the future, Aegis will expose additional env vars (`AEGIS_LLM_API_KEY`, `AEGIS_LLM_PROVIDER`, etc.) — but this is not yet implemented.

### Example: Calling the Gateway

```javascript
const gatewayUrl = process.env.AEGIS_GATEWAY_URL || 'http://localhost:5407';

const response = await fetch(`${gatewayUrl}/v1/chat/completions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
        messages: [{ role: 'user', content: 'Hello' }],
        stream: false,
    }),
});
```

No API key header needed — the gateway injects it.
