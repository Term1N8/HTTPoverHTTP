# Burp Remote Agent — PoC

This is a minimal controller/agent Burp extension plus a dumb HTTP relay.

Architecture:

Controller Burp -> HTTPS/HTTP ->  relay -> HTTPS/HTTP -> Agent Burp -> target

The  relay never connects to the target. It stores pending/completed request messages in SQLite and forwards them between the two Burp extensions.


## Important limitation of v0.1

Start with HTTP/1.x traffic. HTTP/2-specific traffic, WebSockets, streaming bodies, CONNECT, and very large uploads are intentionally not the first milestone.

## Relay

On :

    export RELAY_TOKEN="$(openssl rand -hex 32)"
    export RELAY_HOST=0.0.0.0
    export RELAY_PORT=8080
    python3 relay/relay.py


## Burp

Load the JAR as a Java extension.

Controller:
- Mode = CONTROLLER
- Session = test
- Relay URL = http://_HOST:8080
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

The controller handler suppresses the normal outbound request and returns the response supplied by the agent. The agent reconstructs the request and calls Burp's HTTP API to execute it from the agent host.
