"""Problem domain model, strict request validation and canonical normalization.

A problem is fully determined by its *normalized* form: targets and primers
sorted by ID, independent of the order they appeared in the request.
The SHA-256 of the canonical JSON text is the problem identity used for
idempotency and checkpoint binding.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]{0,31}$")

# Limits from the specification.
MIN_TARGETS, MAX_TARGETS = 1, 256
MIN_PRIMERS, MAX_PRIMERS = 1, 180
MIN_DEMAND, MAX_DEMAND = 1, 3
MIN_RISK, MAX_RISK = 1, 100_000
MIN_VOLUME, MAX_VOLUME = 1, 1_000


class ValidationErrorBundle(Exception):
    """Collects every problem with the request so one 422 lists them all."""

    def __init__(self, errors: List[dict]):
        super().__init__("validation failed")
        self.errors = errors


class _Err(Exception):
    def __init__(self, loc: List[str], msg: str):
        super().__init__(msg)
        self.loc = loc
        self.msg = msg


class PrimerIn(BaseModel):
    id: str
    covers: List[str]
    risk: int
    volume: int
    channel: str


class IncompatiblePairIn(BaseModel):
    a: str = Field(alias="a")
    b: str = Field(alias="b")

    model_config = {"populate_by_name": True}


class PanelJobRequest(BaseModel):
    targets: Dict[str, int]
    primers: List[PrimerIn]
    incompatible_pairs: List[IncompatiblePairIn] = []
    channel_limits: Dict[str, int]
    total_volume_limit: int


class Problem:
    """Normalized, validated problem instance."""

    def __init__(self, raw: PanelJobRequest):
        self.raw = raw
        errors: List[dict] = []

        def fail(loc, msg):
            errors.append({"loc": loc, "msg": msg})

        # ---- targets --------------------------------------------------
        target_ids: List[str] = []
        seen_t = set()
        for name, demand in raw.targets.items():
            if not isinstance(name, str) or not ID_RE.match(name):
                fail(["targets", name], "invalid target id")
                continue
            if name in seen_t:
                fail(["targets", name], "duplicate target id")
                continue
            seen_t.add(name)
            if not isinstance(demand, int) or not (MIN_DEMAND <= demand <= MAX_DEMAND):
                fail(["targets", name], f"demand must be integer in [{MIN_DEMAND},{MAX_DEMAND}]")
                continue
            target_ids.append(name)

        # ---- primers --------------------------------------------------
        primer_order: List[str] = []
        seen_p = set()
        covers: Dict[str, List[str]] = {}
        risks: Dict[str, int] = {}
        volumes: Dict[str, int] = {}
        channels: Dict[str, str] = {}
        for i, p in enumerate(raw.primers):
            loc = ["primers", i]
            ok = True
            if not isinstance(p.id, str) or not ID_RE.match(p.id):
                fail(loc + ["id"], "invalid primer id")
                ok = False
            elif p.id in seen_p:
                fail(loc + ["id"], "duplicate primer id")
                ok = False
            if not isinstance(p.risk, int) or not (MIN_RISK <= p.risk <= MAX_RISK):
                fail(loc + ["risk"], f"risk must be integer in [{MIN_RISK},{MAX_RISK}]")
                ok = False
            if not isinstance(p.volume, int) or not (MIN_VOLUME <= p.volume <= MAX_VOLUME):
                fail(loc + ["volume"], f"volume must be integer in [{MIN_VOLUME},{MAX_VOLUME}]")
                ok = False
            if not isinstance(p.channel, str) or not p.channel:
                fail(loc + ["channel"], "channel must be a non-empty string")
                ok = False
            cset = set()
            if not isinstance(p.covers, list) or len(p.covers) == 0:
                fail(loc + ["covers"], "covers must be a non-empty list")
                ok = False
            else:
                for t in p.covers:
                    if not isinstance(t, str) or not ID_RE.match(t):
                        fail(loc + ["covers"], f"invalid target reference {t!r}")
                        ok = False
                    elif t not in cset:
                        cset.add(t)
                    # duplicates inside one covers list are silently
                    # de-duplicated by set semantics; the spec forbids
                    # "duplicate coverage" -> treat as error:
                if len(cset) != len(p.covers):
                    fail(loc + ["covers"], "duplicate target inside covers")
                    ok = False
                for t in cset:
                    if t not in seen_t:
                        fail(loc + ["covers"], f"unknown target reference {t!r}")
                        ok = False
            if ok:
                seen_p.add(p.id)
                primer_order.append(p.id)
                covers[p.id] = sorted(cset)
                risks[p.id] = p.risk
                volumes[p.id] = p.volume
                channels[p.id] = p.channel

        if not (MIN_PRIMERS <= len(raw.primers) <= MAX_PRIMERS) or any(
            not isinstance(p.id, str) for p in raw.primers
        ):
            fail(["primers"], f"primer count must be in [{MIN_PRIMERS},{MAX_PRIMERS}]")
        if not (MIN_TARGETS <= len(seen_t) <= MAX_TARGETS):
            fail(["targets"], f"target count must be in [{MIN_TARGETS},{MAX_TARGETS}]")

        # ---- channel limits ------------------------------------------
        channel_limits: Dict[str, int] = {}
        for ch, lim in raw.channel_limits.items():
            if not isinstance(ch, str) or not ch:
                fail(["channel_limits", str(ch)], "invalid channel name")
                continue
            if not isinstance(lim, int) or lim < 0:
                fail(["channel_limits", ch], "limit must be a non-negative integer")
                continue
            channel_limits[ch] = lim
        for p_id in primer_order:
            if channels[p_id] not in channel_limits:
                fail(["primers", p_id, "channel"],
                     f"unknown channel {channels[p_id]!r} (missing from channel_limits)")

        # ---- incompatible pairs --------------------------------------
        pair_seen = set()
        for i, pair in enumerate(raw.incompatible_pairs):
            a, b = pair.a, pair.b
            loc = ["incompatible_pairs", i]
            if not isinstance(a, str) or not ID_RE.match(a) or a not in seen_p:
                fail(loc, f"first endpoint {a!r} is not a known primer id")
            if not isinstance(b, str) or not ID_RE.match(b) or b not in seen_p:
                fail(loc, f"second endpoint {b!r} is not a known primer id")
            if isinstance(a, str) and isinstance(b, str) and a == b:
                fail(loc, "self-referential incompatible pair")
            if isinstance(a, str) and isinstance(b, str) and a != b:
                key = tuple(sorted((a, b)))
                if key in pair_seen:
                    fail(loc, "duplicate incompatible pair")
                pair_seen.add(key)

        # ---- total volume limit --------------------------------------
        tvl = raw.total_volume_limit
        if not isinstance(tvl, int) or tvl < 0:
            fail(["total_volume_limit"], "total_volume_limit must be a non-negative integer")

        if errors:
            raise ValidationErrorBundle(errors)

        # Cross constraint: no feasible container if even the smallest
        # primer of a channel cannot fit that channel's cap or the total.
        by_channel: Dict[str, List[str]] = {}
        for p_id in primer_order:
            by_channel.setdefault(channels[p_id], []).append(p_id)
        for ch, members in by_channel.items():
            cap = channel_limits.get(ch)
            if cap is not None and cap < min(1 for _ in members):
                fail(["channel_limits", ch], "limit smaller than any selection (0)")
            if cap is not None and cap <= 0:
                fail(["channel_limits", ch],
                     "channel contains primers but its limit cannot accommodate even one")
        if isinstance(tvl, int):
            min_vol = min(volumes[p] for p in primer_order)
            if tvl < min_vol:
                fail(["total_volume_limit"],
                     "total volume limit cannot accommodate any single candidate")

        if errors:
            raise ValidationErrorBundle(errors)

        # ---- normalized representation --------------------------------
        self.target_ids: List[str] = sorted(target_ids)
        self.demands: Dict[str, int] = {t: raw.targets[t] for t in self.target_ids}
        self.primer_ids: List[str] = sorted(primer_order)
        self.covers = {p: covers[p] for p in self.primer_ids}
        self.risks = {p: risks[p] for p in self.primer_ids}
        self.volumes = {p: volumes[p] for p in self.primer_ids}
        self.channels = {p: channels[p] for p in self.primer_ids}
        self.incompatible_pairs = sorted(pair_seen)
        self.channel_limits = {c: channel_limits[c] for c in sorted(channel_limits)}
        self.total_volume_limit = tvl

        self.canonical = self._canonical()
        self.problem_hash = hashlib.sha256(
            self.canonical.encode("utf-8")
        ).hexdigest()

    def _canonical(self) -> str:
        doc = {
            "targets": {t: self.demands[t] for t in self.target_ids},
            "primers": [
                {
                    "id": p,
                    "covers": self.covers[p],
                    "risk": self.risks[p],
                    "volume": self.volumes[p],
                    "channel": self.channels[p],
                }
                for p in self.primer_ids
            ],
            "incompatible_pairs": [list(x) for x in self.incompatible_pairs],
            "channel_limits": self.channel_limits,
            "total_volume_limit": self.total_volume_limit,
        }
        return json.dumps(doc, sort_keys=True, separators=(",", ":"))

    def to_json(self) -> dict:
        return json.loads(self.canonical)
