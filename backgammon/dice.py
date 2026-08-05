"""backgammon/dice.py -- Dice implementations (public exports).

Exports SeededDice, ScriptedDice, and production_dice factory.

The import rule (§3.3): this module may NOT import asyncio, json, socket,
websockets, random, datetime, pathlib, os, or anything under backgammon.transport.

`secrets` is PERMITTED here, BUT ONLY inside production_dice().
SeededDice.die and ScriptedDice.die must stay pure.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from .types import DiceExhausted


@dataclass(frozen=True, slots=True)
class SeededDice:
    """Deterministic dice from a seed via blake2b."""
    seed: int

    def die(self, n: int) -> int:
        """Return n-th die derived from seed via blake2b."""
        h = hashlib.blake2b(f"{self.seed}:{n}".encode(), digest_size=8).digest()
        return int.from_bytes(h, "big") % 6 + 1


@dataclass(frozen=True, slots=True)
class ScriptedDice:
    """Deterministic dice from a fixed sequence (for testing)."""
    values: tuple[int, ...]

    def die(self, n: int) -> int:
        """Return n-th die from values, or raise DiceExhausted."""
        if n >= len(self.values):
            raise DiceExhausted(n)
        return self.values[n]


def production_dice() -> SeededDice:
    """Factory for production dice: a SeededDice with an unpredictable seed.

    Every game is fully replayable via (seed, moves).
    The seed is never sent on the wire (§4.3.4).
    """
    return SeededDice(secrets.randbits(64))


__all__ = [
    "SeededDice",
    "ScriptedDice",
    "production_dice",
]
