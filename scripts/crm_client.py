#!/usr/bin/env python3
"""Small loopback-only client that imitates the future CRM backend."""

from __future__ import annotations

import argparse
import http.client
import json
import sys
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from local_transcriber.service import CALL_ID_PATTERN, SUPPORTED_EXTENSIONS, extract_call_id  # noqa: E402

MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".aac": "audio/aac",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Loopback CRM integration test client")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    commands = parser.add_subparsers(dest="command", required=True)

    upload = commands.add_parser("upload", help="Upload one local synthetic audio file")
    upload.add_argument("--audio", required=True, type=Path)

    status = commands.add_parser("status", help="Get queue status")
    status.add_argument("--call-id", required=True)

    result = commands.add_parser("result", help="Get completed transcription JSON")
    result.add_argument("--call-id", required=True)
    return parser


def _connection(base_url: str) -> tuple[http.client.HTTPConnection, str]:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise SystemExit("reference client connects only to loopback HTTP")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise SystemExit("base-url must not include query, fragment or credentials")
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=30)
    prefix = parsed.path.rstrip("/")
    return connection, prefix


def _print_response(response: http.client.HTTPResponse) -> int:
    body = response.read()
    try:
        payload = json.loads(body.decode("utf-8"))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except (UnicodeError, json.JSONDecodeError):
        print(body.decode("utf-8", errors="replace"))
    return 0 if 200 <= response.status < 300 else 1


def _upload(base_url: str, audio: Path) -> int:
    if audio.is_symlink() or not audio.is_file():
        raise SystemExit("audio must be an existing regular file, not a symlink")
    extension = audio.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise SystemExit("supported formats: WAV, MP3, FLAC, OGG and AAC")
    call_id = extract_call_id(audio)
    size = audio.stat().st_size
    if size < 1:
        raise SystemExit("audio file is empty")
    connection, prefix = _connection(base_url)
    path = f"{prefix}/v1/jobs/{call_id}/audio{extension}"
    connection.putrequest("PUT", path)
    connection.putheader("Content-Type", MEDIA_TYPES[extension])
    connection.putheader("Content-Length", str(size))
    connection.endheaders()
    with audio.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            connection.send(chunk)
    try:
        return _print_response(connection.getresponse())
    finally:
        connection.close()


def _get(base_url: str, call_id: str, *, result: bool) -> int:
    if not CALL_ID_PATTERN.fullmatch(call_id):
        raise SystemExit("invalid call_id")
    connection, prefix = _connection(base_url)
    suffix = "/result" if result else ""
    connection.request("GET", f"{prefix}/v1/jobs/{call_id}{suffix}")
    try:
        return _print_response(connection.getresponse())
    finally:
        connection.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "upload":
        return _upload(args.base_url, args.audio)
    return _get(args.base_url, args.call_id, result=args.command == "result")


if __name__ == "__main__":
    raise SystemExit(main())
