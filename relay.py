#!/usr/bin/env python3

import json
import os
import secrets
import sqlite3
import threading
import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, parse_qs


HOST = os.getenv("RELAY_HOST", "0.0.0.0")
PORT = int(os.getenv("RELAY_PORT", "9000"))
TOKEN = os.getenv("RELAY_TOKEN", "change-me")
DB = os.getenv("RELAY_DB", "./relay.db")

AGENT_POLL_MAX = 25
CONTROLLER_POLL_MAX = 25

# Requeue jobs if an Agent claims one and disappears.
CLAIM_TIMEOUT = 90

MAX_BODY = 20 * 1024 * 1024

db_lock = threading.Lock()

DB_CONN = sqlite3.connect(
    DB,
    check_same_thread=False
)

DB_CONN.execute("""
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    session TEXT NOT NULL,
    request_b64 TEXT NOT NULL,
    response_b64 TEXT,
    status TEXT NOT NULL,
    created REAL NOT NULL,
    claimed REAL,
    completed REAL,
    error TEXT
)
""")

DB_CONN.commit()


def recover_stale_jobs():
    cutoff = time.time() - CLAIM_TIMEOUT

    with db_lock:
        DB_CONN.execute(
            """
            UPDATE jobs
            SET
                status = 'pending',
                claimed = NULL
            WHERE
                status = 'claimed'
                AND claimed < ?
            """,
            (cutoff,)
        )

        DB_CONN.commit()


def authenticated(handler):
    return (
        handler.headers.get("Authorization", "")
        == "Bearer " + TOKEN
    )


def read_json(handler):
    length = int(
        handler.headers.get(
            "Content-Length",
            "0"
        )
    )

    if length > MAX_BODY:
        raise ValueError("request body too large")

    return json.loads(
        handler.rfile.read(length)
    )


def send_json(handler, code, obj):
    data = json.dumps(obj).encode()

    handler.send_response(code)

    handler.send_header(
        "Content-Type",
        "application/json"
    )

    handler.send_header(
        "Content-Length",
        str(len(data))
    )

    handler.end_headers()

    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(
            "[HTTP] " + (fmt % args),
            flush=True
        )

    def do_POST(self):

        if not authenticated(self):
            send_json(
                self,
                401,
                {"error": "unauthorized"}
            )
            return

        parts = [
            unquote(x)
            for x in urlparse(self.path).path.split("/")
            if x
        ]

        # ------------------------------------------------------
        # Controller -> Relay
        #
        # POST /api/session/<session>/request
        # ------------------------------------------------------

        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "session"
            and parts[3] == "request"
        ):

            session = parts[2]

            try:
                obj = read_json(self)
            except Exception as e:
                send_json(
                    self,
                    400,
                    {"error": str(e)}
                )
                return

            request_b64 = obj.get("request")

            if not request_b64:
                send_json(
                    self,
                    400,
                    {"error": "missing request"}
                )
                return

            job_id = secrets.token_urlsafe(16)

            with db_lock:
                DB_CONN.execute(
                    """
                    INSERT INTO jobs
                    (
                        id,
                        session,
                        request_b64,
                        status,
                        created
                    )
                    VALUES (?, ?, ?, 'pending', ?)
                    """,
                    (
                        job_id,
                        session,
                        request_b64,
                        time.time()
                    )
                )

                DB_CONN.commit()

            print(
                f"[+] QUEUED "
                f"id={job_id} "
                f"session={session}",
                flush=True
            )

            # Return immediately.
            send_json(
                self,
                200,
                {
                    "id": job_id,
                    "status": "pending"
                }
            )

            return

        # ------------------------------------------------------
        # Agent -> Relay
        #
        # POST /api/session/<session>/response/<id>
        # ------------------------------------------------------

        if (
            len(parts) == 5
            and parts[0] == "api"
            and parts[1] == "session"
            and parts[3] == "response"
        ):

            session = parts[2]
            job_id = parts[4]

            try:
                obj = read_json(self)
            except Exception as e:
                send_json(
                    self,
                    400,
                    {"error": str(e)}
                )
                return

            response_b64 = obj.get("response")

            if response_b64 is None:
                send_json(
                    self,
                    400,
                    {"error": "missing response"}
                )
                return

            with db_lock:
                cursor = DB_CONN.execute(
                    """
                    UPDATE jobs
                    SET
                        response_b64 = ?,
                        status = 'complete',
                        completed = ?
                    WHERE
                        id = ?
                        AND session = ?
                    """,
                    (
                        response_b64,
                        time.time(),
                        job_id,
                        session
                    )
                )

                DB_CONN.commit()

            if cursor.rowcount == 0:
                send_json(
                    self,
                    404,
                    {"error": "job not found"}
                )
                return

            print(
                f"[+] COMPLETE "
                f"id={job_id} "
                f"session={session}",
                flush=True
            )

            send_json(
                self,
                200,
                {"ok": True}
            )

            return

        self.send_error(404)

    def do_GET(self):

        if not authenticated(self):
            send_json(
                self,
                401,
                {"error": "unauthorized"}
            )
            return

        parsed = urlparse(self.path)

        parts = [
            unquote(x)
            for x in parsed.path.split("/")
            if x
        ]

        query = parse_qs(parsed.query)

        # ------------------------------------------------------
        # Agent long poll
        #
        # GET /api/session/<session>/next?wait=25
        # ------------------------------------------------------

        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "session"
            and parts[3] == "next"
        ):

            session = parts[2]

            try:
                wait = int(
                    query.get("wait", ["25"])[0]
                )
            except ValueError:
                wait = 25

            wait = min(
                max(wait, 1),
                AGENT_POLL_MAX
            )

            deadline = time.time() + wait

            while time.time() < deadline:

                recover_stale_jobs()

                with db_lock:
                    row = DB_CONN.execute(
                        """
                        SELECT
                            id,
                            request_b64
                        FROM jobs
                        WHERE
                            session = ?
                            AND status = 'pending'
                        ORDER BY created
                        LIMIT 1
                        """,
                        (session,)
                    ).fetchone()

                    if row:

                        DB_CONN.execute(
                            """
                            UPDATE jobs
                            SET
                                status = 'claimed',
                                claimed = ?
                            WHERE
                                id = ?
                                AND status = 'pending'
                            """,
                            (
                                time.time(),
                                row[0]
                            )
                        )

                        DB_CONN.commit()

                        print(
                            f"[>] DISPATCH "
                            f"id={row[0]} "
                            f"session={session}",
                            flush=True
                        )

                        send_json(
                            self,
                            200,
                            {
                                "id": row[0],
                                "request": row[1]
                            }
                        )

                        return

                time.sleep(0.1)

            self.send_response(204)
            self.send_header(
                "Content-Length",
                "0"
            )
            self.end_headers()

            return

        # ------------------------------------------------------
        # Controller waits for result
        #
        # GET /api/session/<session>/response/<id>?wait=25
        # ------------------------------------------------------

        if (
            len(parts) == 5
            and parts[0] == "api"
            and parts[1] == "session"
            and parts[3] == "response"
        ):

            session = parts[2]
            job_id = parts[4]

            try:
                wait = int(
                    query.get("wait", ["25"])[0]
                )
            except ValueError:
                wait = 25

            wait = min(
                max(wait, 1),
                CONTROLLER_POLL_MAX
            )

            deadline = time.time() + wait

            while time.time() < deadline:

                with db_lock:
                    row = DB_CONN.execute(
                        """
                        SELECT
                            status,
                            response_b64,
                            error
                        FROM jobs
                        WHERE
                            id = ?
                            AND session = ?
                        """,
                        (
                            job_id,
                            session
                        )
                    ).fetchone()

                if row is None:
                    send_json(
                        self,
                        404,
                        {"error": "job not found"}
                    )
                    return

                status, response, error = row

                if status == "complete":
                    send_json(
                        self,
                        200,
                        {
                            "id": job_id,
                            "status": "complete",
                            "response": response
                        }
                    )
                    return

                if status == "failed":
                    send_json(
                        self,
                        200,
                        {
                            "id": job_id,
                            "status": "failed",
                            "error": error
                        }
                    )
                    return

                time.sleep(0.1)

            send_json(
                self,
                202,
                {
                    "id": job_id,
                    "status": "pending"
                }
            )

            return

        # ------------------------------------------------------
        # Session status
        # ------------------------------------------------------

        if (
            len(parts) == 3
            and parts[0] == "api"
            and parts[1] == "session"
        ):

            session = parts[2]

            with db_lock:
                rows = DB_CONN.execute(
                    """
                    SELECT
                        status,
                        COUNT(*)
                    FROM jobs
                    WHERE session = ?
                    GROUP BY status
                    """,
                    (session,)
                ).fetchall()

            send_json(
                self,
                200,
                {
                    "session": session,
                    "jobs": dict(rows)
                }
            )

            return

        self.send_error(404)


if __name__ == "__main__":

    print()
    print("[*] Burp Remote Relay")
    print(f"[*] Listening on {HOST}:{PORT}")
    print(f"[*] Database: {DB}")
    print()

    if TOKEN == "change-me":
        print("[!] WARNING: default token")

    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down")
    finally:
        server.server_close()
        DB_CONN.close()
