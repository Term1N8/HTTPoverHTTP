#!/usr/bin/env python3

import argparse
import base64
import json
import os
import secrets
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue
from urllib.parse import parse_qs, urlparse


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_HOST = os.getenv("RELAY_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("RELAY_PORT", "9000"))

# Change this through RELAY_TOKEN or --token.
# Do NOT commit a real token to a public repository.
DEFAULT_TOKEN = os.getenv("RELAY_TOKEN", "CHANGE_ME")

DEFAULT_REQUEST_TIMEOUT = float(
    os.getenv("RELAY_REQUEST_TIMEOUT", "60")
)

DEFAULT_POLL_WAIT = int(
    os.getenv("RELAY_POLL_WAIT", "25")
)

MAX_POLL_WAIT = int(
    os.getenv("RELAY_MAX_POLL_WAIT", "30")
)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

class SessionState:
    def __init__(self):
        self.queue = Queue()
        self.responses = {}
        self.response_events = {}
        self.lock = threading.Lock()


SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


def get_session(name):
    with SESSIONS_LOCK:
        if name not in SESSIONS:
            SESSIONS[name] = SessionState()

        return SESSIONS[name]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def json_bytes(obj):
    return json.dumps(obj).encode("utf-8")


def make_request_id():
    return secrets.token_urlsafe(16)


def read_json(handler):
    raw_length = handler.headers.get("Content-Length")

    if raw_length is None:
        raise ValueError("Missing Content-Length")

    try:
        length = int(raw_length)
    except ValueError:
        raise ValueError("Invalid Content-Length")

    if length <= 0:
        raise ValueError("Missing request body")

    data = handler.rfile.read(length)

    if not data:
        raise ValueError("Empty request body")

    return json.loads(data.decode("utf-8"))


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    server_version = "BurpRemoteRelay/1.0"

    def log_message(self, fmt, *args):
        print(
            f"[HTTP] {self.address_string()} "
            f'"{self.command} {self.path}" '
            f"{fmt % args}",
            flush=True
        )

    def send_json(self, status, obj):

        data = json_bytes(obj)

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "application/json"
        )

        self.send_header(
            "Content-Length",
            str(len(data))
        )

        self.end_headers()

        self.wfile.write(data)

    def send_empty(self, status=204):

        self.send_response(status)

        self.send_header(
            "Content-Length",
            "0"
        )

        self.end_headers()

    def unauthorized(self):

        self.send_response(401)

        self.send_header(
            "Content-Type",
            "text/plain"
        )

        self.send_header(
            "WWW-Authenticate",
            "Bearer"
        )

        self.send_header(
            "Content-Length",
            "0"
        )

        self.end_headers()

    def authenticated(self):

        token = self.server.relay_token

        if not token:
            return True

        supplied = self.headers.get(
            "Authorization",
            ""
        )

        expected = "Bearer " + token

        return secrets.compare_digest(
            supplied,
            expected
        )

    # -----------------------------------------------------------------------
    # GET
    # -----------------------------------------------------------------------

    def do_GET(self):

        if not self.authenticated():

            print(
                "[!] Unauthorized GET:",
                self.path,
                flush=True
            )

            self.unauthorized()
            return

        parsed = urlparse(self.path)

        parts = parsed.path.strip("/").split("/")

        # /api/session/<session>/next
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "session"
            and parts[3] == "next"
        ):

            session_name = parts[2]

            query = parse_qs(
                parsed.query
            )

            try:

                wait = int(
                    query.get(
                        "wait",
                        [DEFAULT_POLL_WAIT]
                    )[0]
                )

            except ValueError:

                wait = DEFAULT_POLL_WAIT

            wait = max(
                1,
                min(wait, MAX_POLL_WAIT)
            )

            self.handle_next(
                session_name,
                wait
            )

            return

        self.send_json(
            404,
            {
                "error": "not found"
            }
        )

    # -----------------------------------------------------------------------
    # POST
    # -----------------------------------------------------------------------

    def do_POST(self):

        if not self.authenticated():

            print(
                "[!] Unauthorized POST:",
                self.path,
                flush=True
            )

            self.unauthorized()
            return

        parsed = urlparse(self.path)

        parts = parsed.path.strip("/").split("/")

        # /api/session/<session>/request
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "session"
            and parts[3] == "request"
        ):

            self.handle_request(
                parts[2]
            )

            return

        # /api/session/<session>/response/<request-id>
        if (
            len(parts) == 5
            and parts[0] == "api"
            and parts[1] == "session"
            and parts[3] == "response"
        ):

            self.handle_response(
                parts[2],
                parts[4]
            )

            return

        self.send_json(
            404,
            {
                "error": "not found"
            }
        )

    # -----------------------------------------------------------------------
    # Controller -> Relay
    # -----------------------------------------------------------------------

    def handle_request(self, session_name):

        try:

            body = read_json(self)

            encoded_request = body.get(
                "request"
            )

            if not encoded_request:
                raise ValueError(
                    "Missing request"
                )

            # Validate Base64.
            base64.b64decode(
                encoded_request,
                validate=True
            )

        except Exception as exc:

            print(
                f"[-] Invalid request: {exc}",
                flush=True
            )

            self.send_json(
                400,
                {
                    "error": str(exc)
                }
            )

            return

        session = get_session(
            session_name
        )

        request_id = make_request_id()

        event = threading.Event()

        with session.lock:

            session.response_events[
                request_id
            ] = event

        session.queue.put(
            {
                "id": request_id,
                "request": encoded_request
            }
        )

        print(
            f"[+] Queued {request_id} "
            f"session={session_name}",
            flush=True
        )

        print(
            f"[>] Waiting for agent "
            f"response {request_id}",
            flush=True
        )

        completed = event.wait(
            timeout=self.server.request_timeout
        )

        with session.lock:

            response = session.responses.pop(
                request_id,
                None
            )

            session.response_events.pop(
                request_id,
                None
            )

        if not completed or response is None:

            print(
                f"[-] Agent timeout "
                f"{request_id}",
                flush=True
            )

            self.send_json(
                504,
                {
                    "error": "agent timeout",
                    "requestId": request_id
                }
            )

            return

        print(
            f"[+] Agent response received "
            f"{request_id}",
            flush=True
        )

        self.send_json(
            200,
            response
        )

    # -----------------------------------------------------------------------
    # Relay -> Agent
    # -----------------------------------------------------------------------

    def handle_next(
        self,
        session_name,
        wait
    ):

        session = get_session(
            session_name
        )

        try:

            job = session.queue.get(
                timeout=wait
            )

        except Empty:

            self.send_empty(204)

            return

        print(
            f"[>] Dispatching "
            f"{job['id']} "
            f"to agent "
            f"session={session_name}",
            flush=True
        )

        self.send_json(
            200,
            job
        )

    # -----------------------------------------------------------------------
    # Agent -> Relay
    # -----------------------------------------------------------------------

    def handle_response(
        self,
        session_name,
        request_id
    ):

        session = get_session(
            session_name
        )

        try:

            body = read_json(self)

        except Exception as exc:

            self.send_json(
                400,
                {
                    "error": str(exc)
                }
            )

            return

        with session.lock:

            event = session.response_events.get(
                request_id
            )

            if event is None:

                print(
                    f"[-] Unknown request "
                    f"{request_id}",
                    flush=True
                )

                self.send_json(
                    404,
                    {
                        "error": "unknown request",
                        "requestId": request_id
                    }
                )

                return

            session.responses[
                request_id
            ] = body

            event.set()

        print(
            f"[<] Response from agent "
            f"{request_id} "
            f"session={session_name}",
            flush=True
        )

        self.send_json(
            200,
            {
                "ok": True
            }
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Burp Remote Agent HTTP/HTTPS relay"
        )
    )

    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Bind address"
    )

    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Listen port"
    )

    parser.add_argument(
        "--token",
        default=DEFAULT_TOKEN,
        help="Bearer authentication token"
    )

    parser.add_argument(
        "--cert",
        help="TLS certificate PEM"
    )

    parser.add_argument(
        "--key",
        help="TLS private key PEM"
    )

    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help="Controller request timeout in seconds"
    )

    args = parser.parse_args()

    if bool(args.cert) != bool(args.key):

        parser.error(
            "--cert and --key must be supplied together"
        )

    if args.token == "CHANGE_ME":

        print(
            "[!] WARNING: using default token "
            "CHANGE_ME",
            flush=True
        )

        print(
            "[!] Set RELAY_TOKEN or use --token "
            "before exposing the relay.",
            flush=True
        )

    server = ThreadingHTTPServer(
        (
            args.host,
            args.port
        ),
        Handler
    )

    # Make configuration available to each request handler.
    server.relay_token = args.token
    server.request_timeout = args.request_timeout

    protocol = "HTTP"

    # -----------------------------------------------------------------------
    # TLS
    # -----------------------------------------------------------------------

    if args.cert and args.key:

        context = ssl.SSLContext(
            ssl.PROTOCOL_TLS_SERVER
        )

        context.minimum_version = (
            ssl.TLSVersion.TLSv1_2
        )

        context.load_cert_chain(
            certfile=args.cert,
            keyfile=args.key
        )

        server.socket = context.wrap_socket(
            server.socket,
            server_side=True
        )

        protocol = "HTTPS"

    # -----------------------------------------------------------------------
    # Startup
    # -----------------------------------------------------------------------

    print()
    print(
        "[+] Burp Remote Agent Relay"
    )

    print(
        f"[+] Listen: {args.host}:{args.port}"
    )

    print(
        f"[+] Protocol: {protocol}"
    )

    print(
        "[+] Bearer authentication: "
        + (
            "enabled"
            if args.token
            else "DISABLED"
        )
    )

    if args.cert:

        print(
            f"[+] TLS certificate: "
            f"{args.cert}"
        )

    print(
        f"[+] Request timeout: "
        f"{args.request_timeout}s"
    )

    print()
    print(
        "[*] Waiting for connections..."
    )
    print()

    try:

        server.serve_forever()

    except KeyboardInterrupt:

        print(
            "\n[*] Shutting down..."
        )

    finally:

        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
