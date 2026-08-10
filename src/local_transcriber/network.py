"""Small defense-in-depth guard against accidental Python-level networking."""

from __future__ import annotations

import os
import socket
from contextlib import ExitStack, contextmanager
from typing import Iterator
from unittest.mock import patch


def configure_offline_environment() -> None:
    """Disable supported Hugging Face telemetry/download behavior before imports."""

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"


def _network_denied(*args: object, **kwargs: object) -> None:
    del args, kwargs
    raise PermissionError("Network access is disabled during local transcription")


@contextmanager
def deny_python_network() -> Iterator[None]:
    """Block common Python socket entry points during model load and inference.

    This does not replace OS/firewall egress controls and cannot constrain native
    code that opens sockets without using Python's socket module.
    """

    configure_offline_environment()
    with ExitStack() as stack:
        stack.enter_context(patch.object(socket, "create_connection", _network_denied))
        stack.enter_context(patch.object(socket, "getaddrinfo", _network_denied))
        stack.enter_context(patch.object(socket.socket, "connect", _network_denied))
        stack.enter_context(patch.object(socket.socket, "connect_ex", _network_denied))
        yield
