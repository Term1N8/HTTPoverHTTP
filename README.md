# HTTPS Relay

Lightweight HTTPS request/response relay between a controller and agents using named sessions.

```text
Controller → Relay → Agent
Controller ← Relay ← Agent
```

## Requirements

* Python 3
* TLS certificate and private key
* No external dependencies

## Setup

Default certificate paths:

```text
certs/server.crt
certs/server.key
```

Generate a self-signed certificate for testing:

```bash
mkdir -p certs

openssl req -x509 -newkey rsa:4096 \
  -keyout certs/server.key \
  -out certs/server.crt \
  -days 365 -nodes \
  -subj "/CN=relay"
```

Start:

```bash
python3 relay.py
```

The relay listens on:

```text
https://0.0.0.0:9000
```

## Configuration

Environment variables:

| Variable      | Default              | Description           |
| ------------- | -------------------- | --------------------- |
| `RELAY_CERT`  | `./certs/server.crt` | TLS certificate       |
| `RELAY_KEY`   | `./certs/server.key` | TLS private key       |
| `RELAY_TOKEN` | unset                | Optional Bearer token |

Example:

```bash
export RELAY_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python3 relay.py
```

When authentication is enabled, clients must send:

```http
Authorization: Bearer <token>
```

## API

### Controller → Relay

Queue a request:

```http
POST /api/session/<session>/request
```

```json
{"request": "..."}
```

The connection waits for the agent response.

### Agent → Relay

Poll for work:

```http
GET /api/session/<session>/next?wait=25
```

Returns:

```json
{
  "id": "<request-id>",
  "request": "..."
}
```

Submit the response:

```http
POST /api/session/<session>/response/<request-id>
```

```json
{
  "result": "..."
}
```

If no request is available, `/next` returns `204`.

## Timeouts

* Controller response timeout: **45 seconds**
* Stale job claim timeout: **60 seconds**
* Agent polling timeout: **1–60 seconds**, default **25 seconds**

Stale jobs are automatically returned to the queue.

## Notes

* Sessions and jobs are stored **in memory**.
* Restarting the relay clears all state.
* TLS 1.2+ is required.
* Use a firewall/network controls to restrict access to port `9000`.
* A Bearer token is recommended for anything beyond a trusted environment.
