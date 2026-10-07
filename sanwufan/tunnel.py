"""Small Python adapter for Tunnelmole's open-source message protocol.

Protocol reference: github.com/robbie-cahill/tunnelmole-client
Only forwards HTTP to the explicitly selected loopback game port.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import re
import secrets
import ssl
import sys
import threading

HOST = re.compile(r"[a-z0-9-]+\.tunnelmole\.(?:net|com)")


def forward_request(message, port):
    url = message.get("url", "")
    result = {"type": "forwardedResponse", "requestId": message.get("requestId"),
              "url": url, "statusCode": 400, "headers": {"content-type": "text/plain"}, "body": ""}
    connection = None
    try:
        if not isinstance(url, str) or not url.startswith("/") or url.startswith("//") or len(url) > 2048:
            return result
        encoded = message.get("body", "")
        if not isinstance(encoded, str) or len(encoded) > 12000:
            result["statusCode"] = 413
            return result
        body = base64.b64decode(encoded, validate=True)
        if len(body) > 8192:
            result["statusCode"] = 413
            return result
        incoming = message.get("headers", {})
        if not isinstance(incoming, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in incoming.items()):
            return result
        headers = {k: v for k, v in incoming.items()
                   if k.lower() not in ("connection", "transfer-encoding", "content-length", "upgrade", "proxy-connection")}
        headers["Connection"] = "close"
        headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request(message.get("method", "GET"), url, body=body, headers=headers)
        response = connection.getresponse()
        response_headers = {}
        for key, value in response.getheaders():
            key = key.lower()
            if key in ("connection", "transfer-encoding"):
                continue
            if key in response_headers:
                existing = response_headers[key]
                response_headers[key] = (existing if isinstance(existing, list) else [existing]) + [value]
            else:
                response_headers[key] = value
        result.update(statusCode=response.status, headers=response_headers,
                      body=base64.b64encode(response.read()).decode("ascii"))
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        result["statusCode"] = 502
    finally:
        if connection:
            connection.close()
    return result


def main():
    import websocket
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Invalid port")
    if not args.identity.exists():
        args.identity.write_text(secrets.token_urlsafe(16), encoding="ascii")
    client_id = args.identity.read_text(encoding="ascii").strip()
    stopped = threading.Event()
    pending = threading.BoundedSemaphore(32)
    ws = None
    try:
        ws = websocket.create_connection("wss://service.tunnelmole.com:8083", timeout=20,
                                         enable_multithread=True,
                                         sslopt={"cert_reqs": ssl.CERT_REQUIRED, "check_hostname": True})
        ws.send(json.dumps({"type": "initialise", "clientId": client_id,
                            "connectionInfo": {"isCli": True, "isNpm": False,
                                               "nodeVersion": "Python adapter", "tunnelmoleVersion": "python-sanwufan-0.5"}}))
        def heartbeat():
            while not stopped.wait(20):
                try:
                    ws.ping()
                except Exception:
                    stopped.set()
                    ws.close()
        threading.Thread(target=heartbeat, daemon=True).start()
        def forward(message):
            try:
                ws.send(json.dumps(forward_request(message, args.port)))
            except (OSError, websocket.WebSocketException):
                stopped.set()
                ws.close()
            finally:
                pending.release()
        with ThreadPoolExecutor(max_workers=8) as pool:
            while not stopped.is_set():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    break
                if len(raw) > 65536:
                    raise ValueError("Oversized relay message")
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("Invalid relay message")
                kind = message.get("type")
                if kind == "hostnameAssigned":
                    hostname = message.get("hostname", "")
                    if not isinstance(hostname, str) or not HOST.fullmatch(hostname):
                        raise ValueError("Invalid relay hostname")
                    print(json.dumps({"type": "sanwufan.tunnel.ready", "origin": "https://" + hostname}), flush=True)
                elif kind == "forwardedRequest":
                    if pending.acquire(blocking=False):
                        pool.submit(forward, message)
                    else:
                        ws.send(json.dumps({"type": "forwardedResponse", "requestId": message.get("requestId"),
                                            "url": message.get("url"), "statusCode": 503, "headers": {}, "body": ""}))
        return 1
    except Exception as exc:
        # Never print relay messages containing cookies, bodies, or private hands.
        print("通道连接结束：" + type(exc).__name__, flush=True)
        return 1
    finally:
        stopped.set()
        if ws:
            ws.close()


if __name__ == "__main__":
    raise SystemExit(main())
