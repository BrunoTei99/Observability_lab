"""Recetor de webhook mínimo: imprime os alertas que o Alertmanager envia."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            payload = json.loads(body)
            for alert in payload.get("alerts", []):
                print(f"[{alert.get('status', '?').upper()}] "
                      f"{alert['labels'].get('alertname')} "
                      f"severity={alert['labels'].get('severity')} - "
                      f"{alert.get('annotations', {}).get('summary', '')}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"Payload inválido: {exc}", flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


print("alert-logger à escuta na porta 5001", flush=True)
HTTPServer(("0.0.0.0", 5001), Handler).serve_forever()
