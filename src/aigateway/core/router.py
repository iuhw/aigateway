"""Model routing: model -> deployments, load balancing, cooldowns, priority tiers.

Borrowed concepts (see docs/research-*.md):
- LiteLLM: deployment cooldown after `allowed_fails` consecutive failures,
  fallback routes, per-route retry policy.
- Higress: priority tiers — lower `priority` numbers are preferred; a higher
  tier is only used when every lower tier is cooling down.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger("aigateway.router")


class RoutingStrategy(StrEnum):
    ROUND_ROBIN = "round_robin"
    WEIGHTED = "weighted"
    LEAST_LATENCY = "least_latency"


_BUCKETS = 10_000


def _hash_bucket(key: str) -> int:
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % _BUCKETS


@dataclass
class RoutePolicy:
    timeout_s: float = 60.0
    retries: int = 2
    retry_backoff_s: float = 0.5
    cooldown_s: float = 30.0
    allowed_fails: int = 2
    strategy: RoutingStrategy = RoutingStrategy.ROUND_ROBIN
    failover_on_client_error: bool = False


@dataclass
class CanaryRule:
    """Gradual-traffic-shift rule (blueprint M5): a share of requests for the
    owning model route is diverted to the `target` model route.

    Splitting is sticky per identity: the identity name is hashed into 10,000
    buckets and the first `percent`% go to the target, so a given key always
    lands on the same branch (salt reshuffles the assignment without moving
    the ratio). Requests without a usable identity are split per-request by
    chance. `keys` force-lists identities into the canary; `exclude_keys`
    pins them to stable (wins over `keys`)."""

    target: str
    percent: float = 0.0
    keys: list[str] = field(default_factory=list)
    exclude_keys: list[str] = field(default_factory=list)
    salt: str = ""

    def snapshot(self) -> dict:
        return {
            "target": self.target,
            "percent": self.percent,
            "keys": list(self.keys),
            "exclude_keys": list(self.exclude_keys),
            "salt": self.salt,
        }


@dataclass
class Deployment:
    provider: str
    model: str
    weight: int = 1
    priority: int = 0

    # --- runtime state (mutable) ---
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    ema_latency_s: float | None = None
    total_requests: int = 0
    total_successes: int = 0
    total_failures: int = 0

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"

    def is_cooling_down(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) < self.cooldown_until

    def snapshot(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "weight": self.weight,
            "priority": self.priority,
            "healthy": not self.is_cooling_down(),
            "cooldown_until_remaining_s": max(0.0, self.cooldown_until - time.monotonic()),
            "consecutive_failures": self.consecutive_failures,
            "ema_latency_s": self.ema_latency_s,
            "total_requests": self.total_requests,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
        }


@dataclass
class ModelRoute:
    name: str
    deployments: list[Deployment]
    policy: RoutePolicy = field(default_factory=RoutePolicy)
    fallbacks: list[str] = field(default_factory=list)
    canary: CanaryRule | None = None

    def __post_init__(self) -> None:
        self._rr_cursor = 0

    def snapshot(self) -> dict:
        return {
            "model": self.name,
            "deployments": [d.snapshot() for d in self.deployments],
            "policy": {
                "strategy": str(self.policy.strategy),
                "timeout_s": self.policy.timeout_s,
                "retries": self.policy.retries,
                "cooldown_s": self.policy.cooldown_s,
                "allowed_fails": self.policy.allowed_fails,
            },
            "fallbacks": self.fallbacks,
            "canary": self.canary.snapshot() if self.canary else None,
        }


class Router:
    """Selects deployments for a model and tracks their health."""

    def __init__(self, routes: dict[str, ModelRoute]):
        self.routes = routes

    def route(self, model: str) -> ModelRoute | None:
        return self.routes.get(model)

    def resolve_canary(self, model: str, *, identity: str | None = None) -> tuple[str, bool | None]:
        """Apply the model's canary rule. Returns (effective_model, branch)
        where branch is None (no rule), False (stable) or True (canary)."""
        route = self.routes.get(model)
        rule = route.canary if route is not None else None
        if rule is None:
            return model, None
        if rule.target not in self.routes:
            # Validation normally prevents this; a stale rule after a hot
            # reload race must not take requests down.
            logger.warning(
                "canary target %r for model %r is not configured; serving stable", rule.target, model
            )
            return model, False
        if identity and identity in rule.exclude_keys:
            return model, False
        if identity and identity in rule.keys:
            return rule.target, True
        if rule.percent >= 100:
            return rule.target, True
        if rule.percent <= 0:
            return model, False
        bucket = (
            _hash_bucket(f"{rule.salt}\x1f{model}\x1f{identity}")
            if identity and identity != "anonymous"
            else random.randrange(_BUCKETS)
        )
        if bucket < int(round(rule.percent * (_BUCKETS / 100))):
            return rule.target, True
        return model, False

    def model_names(self) -> list[str]:
        return sorted(self.routes)

    def pick(self, route: ModelRoute, *, exclude: set[str] | None = None) -> Deployment | None:
        """Pick the next healthy deployment, or None when the route is exhausted."""
        exclude = exclude or set()
        now = time.monotonic()
        available = [
            d
            for d in route.deployments
            if d.key not in exclude and not d.is_cooling_down(now)
        ]
        if not available:
            return None
        best_tier = min(d.priority for d in available)
        tier = [d for d in available if d.priority == best_tier]
        strategy = route.policy.strategy
        if strategy == RoutingStrategy.WEIGHTED:
            weights = [max(1, d.weight) for d in tier]
            return random.choices(tier, weights=weights, k=1)[0]
        if strategy == RoutingStrategy.LEAST_LATENCY:
            return min(tier, key=lambda d: d.ema_latency_s if d.ema_latency_s is not None else 0.0)
        # round robin
        tier.sort(key=lambda d: d.key)
        index = route._rr_cursor % len(tier)
        route._rr_cursor += 1
        return tier[index]

    def report_success(self, deployment: Deployment, latency_s: float) -> None:
        deployment.total_requests += 1
        deployment.total_successes += 1
        deployment.consecutive_failures = 0
        alpha = 0.3
        if deployment.ema_latency_s is None:
            deployment.ema_latency_s = latency_s
        else:
            deployment.ema_latency_s = alpha * latency_s + (1 - alpha) * deployment.ema_latency_s

    def report_failure(self, deployment: Deployment, route: ModelRoute) -> None:
        deployment.total_requests += 1
        deployment.total_failures += 1
        deployment.consecutive_failures += 1
        if deployment.consecutive_failures >= route.policy.allowed_fails:
            deployment.cooldown_until = time.monotonic() + route.policy.cooldown_s
            deployment.consecutive_failures = 0

    # -- cross-instance cooldown sync --------------------------------------------
    # Cooldowns are process-local for the hot path; a background task exports
    # active cooldowns to the shared backend and adopts remote ones (eventual
    # consistency, ~seconds). Exactness is not required: every instance fails
    # over independently, sharing only avoids re-hammering dead upstreams.

    def export_cooldowns(self) -> list[tuple[str, str, float]]:
        """Active cooldowns as (model, deployment_key, remaining_seconds)."""
        now = time.monotonic()
        result: list[tuple[str, str, float]] = []
        for route in self.routes.values():
            for deployment in route.deployments:
                remaining = deployment.cooldown_until - now
                if remaining > 0:
                    result.append((route.name, deployment.key, remaining))
        return result

    def impose_cooldown(self, model: str, deployment_key: str, remaining_s: float) -> bool:
        """Adopt a remote cooldown; returns True when it extended local state."""
        route = self.routes.get(model)
        if route is None:
            return False
        for deployment in route.deployments:
            if deployment.key == deployment_key:
                local_remaining = deployment.cooldown_until - time.monotonic()
                if remaining_s > local_remaining:
                    deployment.cooldown_until = time.monotonic() + remaining_s
                    return True
                return False
        return False
