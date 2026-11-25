import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

CLOUD_DB = "mock_cloud_store.db"


def init_cloud_db():
    conn = sqlite3.connect(CLOUD_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS synced_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT UNIQUE,
            entity_type TEXT,
            payload_json TEXT,
            created_at TEXT,
            received_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


class CloudSyncHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "online"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/sync":
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            received_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            conn = sqlite3.connect(CLOUD_DB)
            cur = conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO synced_events (idempotency_key, entity_type, payload_json, created_at, received_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    payload["idempotency_key"],
                    payload["entity_type"],
                    payload["payload_json"],
                    payload["created_at"],
                    received_at,
                ),
            )
            inserted = cur.rowcount == 1
            conn.commit()
            conn.close()

            print(f"[SYNC] key={payload['idempotency_key']} type={payload['entity_type']} new={inserted}")
            self._send_json(200, {"status": "ok", "deduplicated": not inserted})
        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    init_cloud_db()
    print("Mock Cloud Receiver listening on http://127.0.0.1:8001 (100% stdlib, zero external deps)")
    HTTPServer(("127.0.0.1", 8001), CloudSyncHandler).serve_forever()
