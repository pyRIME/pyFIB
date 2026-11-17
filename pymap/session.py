"""Persist a RegistrationSession to / from JSON so an overlay is reproducible."""

from __future__ import annotations

import json
from .model import RegistrationSession


def save_session(session: RegistrationSession, path: str) -> None:
    with open(path, "w") as f:
        json.dump(session.to_dict(), f, indent=2)


def load_session(path: str) -> RegistrationSession:
    with open(path, "r") as f:
        return RegistrationSession.from_dict(json.load(f))
