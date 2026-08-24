#!/usr/bin/env python3
"""Local, scoped Rabbit R1 voice bridge for Jacco Console."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8787
ALLOWED_ORIGIN = "https://jajajack.github.io"
MAX_AUDIO_BYTES = 10 * 1024 * 1024
PAIR_TTL_SECONDS = 10 * 60
MAX_PAIR_ATTEMPTS = 10
WHISPER_MODEL = os.environ.get("JACCO_WHISPER_MODEL", "small")
FFMPEG = "/opt/homebrew/bin/ffmpeg"
WHISPER = "/opt/homebrew/bin/whisper"
OPENCLAW_URL = "http://127.0.0.1:18789/v1/chat/completions"
CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"
BRIDGE_TOKEN_PATH = Path.home() / ".rabbit-console-bridge-token"

MODE_PROMPTS = {
    "werk": (
        "Je bent Jacco's beleidsassistent voor criminaliteit, cybersecurity, "
        "veilig ondernemen en betalingsverkeer. Geef eerst het antwoord en daarna "
        "maximaal vijf belangen, risico's of blinde vlekken."
    ),
    "scriptie": (
        "Je bent een kritische academische sparringpartner voor Jacco's onderzoek "
        "naar de overgang van publiek-private netwerksamenwerking naar "
        "cybersecurity-ecosystemen. Scheid argumenten, aannames, bewijs en vragen."
    ),
    "thuis": (
        "Je helpt Jacco met zijn Mac mini, Synology en dagelijkse routines. "
        "Geef een kort, praktisch en stapsgewijs antwoord."
    ),
    "voertuigen": (
        "Je helpt Jacco met zijn BMW, Honda Africa Twin RD07 uit 1993 en Suzuki "
        "DR650 RS uit 1991. Benoem onzekerheid en begin met veilige controles."
    ),
}

SAFETY_PROMPT = (
    "Deze voice-console draait voorlopig in alleen-lezen-modus. Voer geen externe "
    "schrijfhandelingen uit: verstuur niets, verwijder niets, koop niets, wijzig geen "
    "accounts of configuraties en bestuur geen fysieke apparaten. Geef alleen "
    "informatie, analyse, concepttekst of een voorstel voor vervolgstappen."
)


def load_or_create_bridge_token() -> str:
    if BRIDGE_TOKEN_PATH.exists():
        return BRIDGE_TOKEN_PATH.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    BRIDGE_TOKEN_PATH.write_text(token, encoding="utf-8")
    BRIDGE_TOKEN_PATH.chmod(0o600)
    return token


def load_openclaw_token() -> str:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    token = config.get("gateway", {}).get("auth", {}).get("token")
    if not token:
        raise RuntimeError("OpenClaw gateway token ontbreekt")
    return token


BRIDGE_TOKEN = load_or_create_bridge_token()
PAIR_CODE = f"{secrets.randbelow(100_000_000):08d}"
PAIR_EXPIRES_AT = time.time() + PAIR_TTL_SECONDS
pair_attempts = 0
pair_lock = threading.Lock()


def transcribe(audio: bytes, content_type: str) -> str:
    suffix = ".webm"
    if "mp4" in content_type:
        suffix = ".m4a"
    elif "wav" in content_type:
        suffix = ".wav"

    with tempfile.TemporaryDirectory(prefix="rabbit-console-") as temp_dir:
        temp = Path(temp_dir)
        source = temp / f"input{suffix}"
        wav = temp / "speech.wav"
        source.write_bytes(audio)

        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
             "-ar", "16000", "-ac", "1", str(wav)],
            check=True,
            timeout=60,
        )
        subprocess.run(
            [WHISPER, str(wav), "--model", WHISPER_MODEL, "--language", "nl",
             "--output_format", "txt", "--output_dir", str(temp),
             "--fp16", "False", "--verbose", "False"],
            check=True,
            timeout=300,
        )
        transcript_path = temp / "speech.txt"
        transcript = transcript_path.read_text(encoding="utf-8").strip()
        if not transcript:
            raise RuntimeError("Geen spraak herkend")
        return transcript


def ask_openclaw(mode: str, transcript: str) -> str:
    prompt = MODE_PROMPTS.get(mode)
    if prompt is None:
        raise ValueError("Onbekende stand")
    payload = {
        "model": "openclaw/default",
        "user": f"rabbit-console-{mode}",
        "messages": [
            {"role": "system", "content": f"{prompt}\n\n{SAFETY_PROMPT}"},
            {"role": "user", "content": transcript},
        ],
    }
    request = urllib.request.Request(
        OPENCLAW_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {load_openclaw_token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.load(response)
    content = result["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "\n".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        )
    return str(content).strip()


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "JaccoConsoleBridge/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin == ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
            self.send_header("Vary", "Origin")

    def _json(self, status: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Console-Mode")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"ok": True, "service": "jacco-console-bridge"})
        else:
            self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if self.path == "/pair":
            self._pair()
        elif self.path == "/command":
            self._command()
        else:
            self._json(404, {"ok": False, "error": "not_found"})

    def _read_body(self, limit: int) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > limit:
            raise ValueError("Ongeldige berichtgrootte")
        return self.rfile.read(length)

    def _pair(self) -> None:
        global pair_attempts
        try:
            supplied = json.loads(self._read_body(2048))["code"]
        except Exception:
            self._json(400, {"ok": False, "error": "invalid_request"})
            return
        with pair_lock:
            pair_attempts += 1
            allowed = (
                time.time() <= PAIR_EXPIRES_AT
                and pair_attempts <= MAX_PAIR_ATTEMPTS
                and secrets.compare_digest(str(supplied), PAIR_CODE)
            )
        if not allowed:
            self._json(403, {"ok": False, "error": "pairing_failed"})
            return
        self._json(200, {"ok": True, "token": BRIDGE_TOKEN})

    def _command(self) -> None:
        auth = self.headers.get("Authorization", "")
        if not secrets.compare_digest(auth, f"Bearer {BRIDGE_TOKEN}"):
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        mode = self.headers.get("X-Console-Mode", "").lower()
        if mode not in MODE_PROMPTS:
            self._json(400, {"ok": False, "error": "invalid_mode"})
            return
        try:
            audio = self._read_body(MAX_AUDIO_BYTES)
            transcript = transcribe(audio, self.headers.get("Content-Type", ""))
            answer = ask_openclaw(mode, transcript)
            self._json(200, {"ok": True, "transcript": transcript, "answer": answer})
        except subprocess.TimeoutExpired:
            self._json(504, {"ok": False, "error": "processing_timeout"})
        except urllib.error.HTTPError as exc:
            self._json(502, {"ok": False, "error": "openclaw_http_error", "status": exc.code})
        except Exception as exc:
            print(f"Bridgefout: {type(exc).__name__}: {exc}")
            self._json(500, {"ok": False, "error": "processing_failed"})


if __name__ == "__main__":
    print(f"Jacco Console Bridge luistert op http://{HOST}:{PORT}")
    print(f"Tijdelijke koppelcode (10 minuten): {PAIR_CODE}")
    print(f"Whisper-model: {WHISPER_MODEL}")
    ThreadingHTTPServer((HOST, PORT), BridgeHandler).serve_forever()
