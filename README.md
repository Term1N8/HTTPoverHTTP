# Burp Remote Agent — PoC

This is a minimal controller/agent Burp extension plus a dumb HTTP relay.

Architecture:

Controller Burp -> HTTPS/HTTP -> EC2 relay -> HTTPS/HTTP -> Agent Burp -> target

The EC2 relay never connects to the target. It stores pending/completed request messages in SQLite and forwards them between the two Burp extensions.

## What this PoC supports

- Controller / Agent mode in one JAR
- Shared session string
- Bearer token authentication
- Long-polling agent
- Raw HTTP request/response transport
- Controller interception of Burp outbound HTTP requests
- Agent execution using Burp's Montoya HTTP API
- SQLite-backed relay queue
- Binary-safe request/response bodies via base64

## Important limitation of v0.1

Start with HTTP/1.x traffic. HTTP/2-specific traffic, WebSockets, streaming bodies, CONNECT, and very large uploads are intentionally not the first milestone.

## Build

Requirements:
- JDK 21
- Maven
- Burp Suite supporting Montoya API 2026.7

Run:

    mvn package

The shaded JAR is:

    target/burp-remote-agent-0.1.0.jar

PortSwigger currently documents Java 21 (or lower) for extensions and Maven/Gradle setup for Montoya. The project uses Montoya API 2026.7.

## Relay

On EC2:

    export RELAY_TOKEN="$(openssl rand -hex 32)"
    export RELAY_HOST=0.0.0.0
    export RELAY_PORT=8080
    python3 relay/relay.py

For the initial test, use HTTP:

    http://EC2_HOST:8080

Once the message flow works, put nginx/Caddy in front of it and expose only HTTPS/443 to the VDI.

Do NOT expose the relay with the default token.

## Burp

Load the JAR as a Java extension.

Controller:
- Mode = CONTROLLER
- Session = test
- Relay URL = http://EC2_HOST:8080
- Token = same RELAY_TOKEN
- Enable remote routing = checked

Agent:
- Mode = AGENT
- Session = test
- Relay URL = same
- Token = same
- Enable remote routing = checked
- Click Apply

Then browse to an HTTP/1.x target from the controller Burp.

The controller handler suppresses the normal outbound request and returns the response supplied by the agent. The agent reconstructs the request and calls Burp's HTTP API to execute it from the VDI.

## Next improvements

1. HTTPS relay on 443.
2. Better error/timeout responses.
3. HTTP/2 support.
4. Request size limits and compression.
5. Multiple agents per session.
6. Controller UI showing queued/completed requests.
7. Optional "route only in-scope traffic".
8. WebSocket support.
9. File-upload/streaming handling.
