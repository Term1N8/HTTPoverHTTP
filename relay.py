#!/usr/bin/env python3

import base64
import json
import os
import secrets
import ssl
import sys
import threading
import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote


HOST = "0.0.0.0"
PORT = 9000

CERT = os.environ.get("RELAY_CERT", "./certs/server.crt")
KEY = os.environ.get("RELAY_KEY", "./certs/server.key")

TOKEN = os.environ.get("RELAY_TOKEN", "")

REQUEST_TIMEOUT = 45
CLAIM_TIMEOUT = 60


# ----------------------------------------------------------------------
# State
# ----------------------------------------------------------------------

lock = threading.Condition()

sessions = {}

# sessions[session] = {
#     "queue": [],
#     "jobs": {
#         id: {
#             "request": base64 string,
#             "created": timestamp,
#             "claimed": timestamp or None,
#             "response": dict or None,
#         }
#     }
# }


def get_session(name):
    with lock:
        if name not in sessions:
            sessions[name] = {
                "queue": [],
                "jobs": {},
            }

        return sessions[name]


def log(message):
    print(
        time.strftime("%Y-%m-%d %H:%M:%S"),
        message,
        flush=True,
    )


def new_id():
    return secrets.token_urlsafe(16)


# ----------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(
            "[HTTP] "
            + self.address_string()
            + " "
            + fmt % args
        )

    def send_json(self, status, obj):
        body = json.dumps(obj).encode()

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json"
        )
        self.send_header(
            "Content-Length",
            str(len(body))
        )
        self.send_header(
            "Connection",
            "keep-alive"
        )
        self.end_headers()

        self.wfile.write(body)

    def send_empty(self, status=204):
        self.send_response(status)
        self.send_header(
            "Content-Length",
            "0"
        )
        self.send_header(
            "Connection",
            "keep-alive"
        )
        self.end_headers()

    def authorized(self):

        if not TOKEN:
            return True

        supplied = self.headers.get(
            "Authorization",
            ""
        )

        return supplied == "Bearer " + TOKEN

    def read_json(self):

        length = int(
            self.headers.get(
                "Content-Length",
                "0"
            )
        )

        if length <= 0:
            return {}

        raw = self.rfile.read(length)

        return json.loads(
            raw.decode("utf-8")
        )

    def route(self):

        parsed = urlparse(self.path)

        parts = [
            unquote(x)
            for x in parsed.path.split("/")
            if x
        ]

        query = parse_qs(parsed.query)

        return parts, query

    # --------------------------------------------------------------
    # GET
    # --------------------------------------------------------------

    def do_GET(self):

        if not self.authorized():
            log("[!] Unauthorized GET from " + self.client_address[0])
            self.send_json(
                401,
                {"error": "unauthorized"}
            )
            return

        parts, query = self.route()

        log(
            "[GET] "
            + self.client_address[0]
            + " "
            + self.path
        )

        # /api/session/<session>/next
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "session"
            and parts[3] == "next"
        ):

            session_name = parts[2]

            wait = 25

            try:
                wait = min(
                    60,
                    max(
                        1,
                        int(
                            query.get(
                                "wait",
                                ["25"]
                            )[0]
                        )
                    )
                )
            except Exception:
                pass

            self.handle_next(
                session_name,
                wait
            )

            return

        self.send_json(
            404,
            {"error": "not found"}
        )

    # --------------------------------------------------------------
    # POST
    # --------------------------------------------------------------

    def do_POST(self):

        if not self.authorized():
            log("[!] Unauthorized POST from " + self.client_address[0])
            self.send_json(
                401,
                {"error": "unauthorized"}
            )
            return

        parts, query = self.route()

        log(
            "[POST] "
            + self.client_address[0]
            + " "
            + self.path
        )

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

        # /api/session/<session>/response/<id>
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
            {"error": "not found"}
        )

    # --------------------------------------------------------------
    # Controller -> relay
    # --------------------------------------------------------------

    def handle_request(self, session_name):

        try:
            data = self.read_json()

            raw_request = data.get(
                "request"
            )

            if not raw_request:
                self.send_json(
                    400,
                    {"error": "missing request"}
                )
                return

            request_id = new_id()

            session = get_session(
                session_name
            )

            job = {
                "id": request_id,
                "request": raw_request,
                "created": time.time(),
                "claimed": None,
                "response": None,
            }

            with lock:

                session["jobs"][request_id] = job
                session["queue"].append(
                    request_id
                )

                log(
                    "[+] Queued "
                    + request_id
                    + " session="
                    + session_name
                )

                lock.notify_all()

            # Wait for Agent response.

            deadline = (
                time.time()
                + REQUEST_TIMEOUT
            )

            with lock:

                while True:

                    if job["response"] is not None:

                        response = job[
                            "response"
                        ]

                        log(
                            "[+] Completed "
                            + request_id
                            + " session="
                            + session_name
                        )

                        self.send_json(
                            200,
                            response
                        )

                        return

                    remaining = (
                        deadline
                        - time.time()
                    )

                    if remaining <= 0:
                        break

                    lock.wait(
                        timeout=min(
                            remaining,
                            1
                        )
                    )

            log(
                "[-] Timeout "
                + request_id
                + " session="
                + session_name
            )

            self.send_json(
                504,
                {
                    "error": "agent timeout",
                    "requestId": request_id,
                }
            )

        except Exception as e:

            log(
                "[!] Request error: "
                + repr(e)
            )

            self.send_json(
                500,
                {
                    "error": str(e)
                }
            )

    # --------------------------------------------------------------
    # Agent polling
    # --------------------------------------------------------------

    def handle_next(
        self,
        session_name,
        wait
    ):

        session = get_session(
            session_name
        )

        deadline = (
            time.time()
            + wait
        )

        while True:

            with lock:

                # Remove stale claims.

                now = time.time()

                for job in session[
                    "jobs"
                ].values():

                    if (
                        job["claimed"]
                        and job["response"] is None
                        and now - job["claimed"]
                        > CLAIM_TIMEOUT
                    ):

                        log(
                            "[!] Releasing stale job "
                            + job["id"]
                        )

                        job["claimed"] = None

                        if job["id"] not in session[
                            "queue"
                        ]:

                            session[
                                "queue"
                            ].append(
                                job["id"]
                            )

                if session["queue"]:

                    request_id = session[
                        "queue"
                    ].pop(0)

                    job = session[
                        "jobs"
                    ].get(request_id)

                    if job is None:
                        continue

                    job["claimed"] = time.time()

                    log(
                        "[>] Dispatching "
                        + request_id
                        + " to agent session="
                        + session_name
                    )

                    self.send_json(
                        200,
                        {
                            "id": request_id,
                            "request": job[
                                "request"
                            ],
                        }
                    )

                    return

                remaining = (
                    deadline
                    - time.time()
                )

                if remaining <= 0:

                    self.send_empty(204)

                    return

                lock.wait(
                    timeout=min(
                        remaining,
                        1
                    )
                )

    # --------------------------------------------------------------
    # Agent -> relay
    # --------------------------------------------------------------

    def handle_response(
        self,
        session_name,
        request_id
    ):

        try:

            data = self.read_json()

            session = get_session(
                session_name
            )

            with lock:

                job = session[
                    "jobs"
                ].get(request_id)

                if job is None:

                    log(
                        "[!] Response for unknown job "
                        + request_id
                    )

                    self.send_json(
                        404,
                        {"error": "unknown request"}
                    )

                    return

                job["response"] = data

                log(
                    "[+] Response received "
                    + request_id
                    + " session="
                    + session_name
                )

                lock.notify_all()

            self.send_json(
                200,
                {
                    "ok": True,
                    "requestId": request_id,
                }
            )

        except Exception as e:

            log(
                "[!] Response error: "
                + repr(e)
            )

            self.send_json(
                500,
                {
                    "error": str(e)
                }
            )


# ----------------------------------------------------------------------
# TLS server
# ----------------------------------------------------------------------

def main():

    if not os.path.exists(CERT):
        print(
            "[-] Certificate not found: "
            + CERT
        )
        print(
            "[-] Generate it first."
        )
        sys.exit(1)

    if not os.path.exists(KEY):
        print(
            "[-] Private key not found: "
            + KEY
        )
        sys.exit(1)

    log(
        "[*] Starting HTTPS relay"
    )

    log(
        "[*] Listening on "
        + HOST
        + ":"
        + str(PORT)
    )

    log(
        "[*] Certificate: "
        + CERT
    )

    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler
    )

    context = ssl.SSLContext(
        ssl.PROTOCOL_TLS_SERVER
    )

    context.minimum_version = (
        ssl.TLSVersion.TLSv1_2
    )

    context.load_cert_chain(
        certfile=CERT,
        keyfile=KEY
    )

    server.socket = context.wrap_socket(
        server.socket,
        server_side=True
    )

    log(
        "[+] HTTPS/TLS enabled"
    )

    try:

        server.serve_forever()

    except KeyboardInterrupt:

        log(
            "[*] Shutting down..."
        )

    finally:

        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
