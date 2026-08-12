"""Agent model and effort catalog with tolerant value resolution.

The harness passes ``model`` and ``effort`` straight to the provider CLI, so a
mistyped alias used to survive every preflight check and only surface as
``agent exited 1`` once the worker was already running. This module resolves
requested values against the catalog of values that provider actually accepts,
choosing the most likely candidate instead of failing late.

Resolution never changes a provider: the permission feasibility report proves
capabilities per provider, and silently switching one would bypass that proof.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .models import AgentProvider


CATALOG_FILENAME = "agent-catalog.json"
CODEX_MODEL_CACHE = ".codex/models_cache.json"
CODEX_CONFIG = ".codex/config.toml"

EFFORT_LADDER = ("low", "medium", "high", "xhigh", "max", "ultra")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")

EFFORT_ALIASES = {
    "mid": "medium",
    "med": "medium",
    "normal": "medium",
    "standard": "medium",
    "default": "medium",
    "balanced": "medium",
    "hi": "high",
    "xhi": "xhigh",
    "xh": "xhigh",
    "extrahigh": "xhigh",
    "veryhigh": "xhigh",
    "superhigh": "xhigh",
    "min": "low",
    "minimal": "low",
    "lowest": "low",
    "fast": "low",
    "quick": "low",
    "maximum": "max",
    "highest": "max",
    "full": "max",
}

METHOD_EXACT = "exact"
METHOD_ALIAS = "alias"
METHOD_NORMALIZED = "normalized"
METHOD_FUZZY = "fuzzy"
METHOD_CLAMPED = "clamped"
METHOD_DEFAULT = "default"
METHOD_INHERIT = "inherit"

TOLERANT_METHODS = frozenset(
    {METHOD_FUZZY, METHOD_CLAMPED, METHOD_DEFAULT}
)

FUZZY_CUTOFF = 0.6


class CatalogError(RuntimeError):
    """Base error for agent catalog handling."""


class UnknownAgentValueError(CatalogError):
    """Raised in strict mode when a value needs a tolerant fallback."""


@dataclass(frozen=True)
class ModelEntry:
    canonical: str
    aliases: tuple[str, ...]
    efforts: tuple[str, ...]
    default_effort: str | None
    hidden: bool = False


@dataclass(frozen=True)
class AgentCatalog:
    models: Mapping[AgentProvider, tuple[ModelEntry, ...]]
    default_model: Mapping[AgentProvider, str]
    sources: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def entries(self, provider: AgentProvider) -> tuple[ModelEntry, ...]:
        return tuple(self.models.get(provider, ()))

    def entry(
        self,
        provider: AgentProvider,
        canonical: str | None,
    ) -> ModelEntry | None:
        if canonical is None:
            return None
        for item in self.entries(provider):
            if item.canonical == canonical:
                return item
        return None

    def model_names(self, provider: AgentProvider) -> tuple[str, ...]:
        return tuple(item.canonical for item in self.entries(provider))


@dataclass(frozen=True)
class ResolvedValue:
    requested: str | None
    value: str | None
    method: str
    warning: str | None = None

    @property
    def tolerant(self) -> bool:
        return self.method in TOLERANT_METHODS


@dataclass(frozen=True)
class ResolvedAgent:
    provider: AgentProvider
    model: ResolvedValue
    effort: ResolvedValue

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(
            item
            for item in (self.model.warning, self.effort.warning)
            if item
        )


STATIC_CLAUDE_MODELS = (
    ModelEntry("opus", ("opus5", "opus-5"), CLAUDE_EFFORTS, "high"),
    ModelEntry("sonnet", ("sonnet5", "sonnet-5"), CLAUDE_EFFORTS, "medium"),
    ModelEntry("fable", ("fable5", "fable-5"), CLAUDE_EFFORTS, "high"),
    ModelEntry("haiku", ("haiku45", "haiku-4-5"), CLAUDE_EFFORTS, "low"),
    ModelEntry("claude-opus-5", (), CLAUDE_EFFORTS, "high"),
    ModelEntry("claude-sonnet-5", (), CLAUDE_EFFORTS, "medium"),
    ModelEntry("claude-fable-5", (), CLAUDE_EFFORTS, "high"),
    ModelEntry(
        "claude-haiku-4-5-20251001",
        (),
        CLAUDE_EFFORTS,
        "low",
    ),
)

STATIC_CODEX_MODELS = (
    ModelEntry("gpt-5.6-terra", ("terra",), CODEX_EFFORTS, "medium"),
    ModelEntry("gpt-5.6-sol", ("sol",), CODEX_EFFORTS, "low"),
    ModelEntry("gpt-5.6-luna", ("luna",), CODEX_EFFORTS[:5], "medium"),
    ModelEntry("gpt-5.5", (), CODEX_EFFORTS[:4], "medium"),
    ModelEntry("gpt-5.4", (), CODEX_EFFORTS[:4], "medium"),
    ModelEntry("gpt-5.4-mini", (), CODEX_EFFORTS[:4], "medium"),
)

STATIC_DEFAULT_MODEL = {
    AgentProvider.CLAUDE: "sonnet",
    AgentProvider.CODEX: "gpt-5.6-terra",
}


def normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _dedupe_aliases(
    entries: tuple[ModelEntry, ...],
) -> tuple[ModelEntry, ...]:
    """Drop aliases claimed by more than one model, and self-shadowing ones."""
    canonicals = {item.canonical for item in entries}
    counts: dict[str, int] = {}
    for item in entries:
        for alias in item.aliases:
            counts[alias] = counts.get(alias, 0) + 1
    return tuple(
        replace(
            item,
            aliases=tuple(
                alias
                for alias in item.aliases
                if counts.get(alias, 0) == 1 and alias not in canonicals
            ),
        )
        for item in entries
    )


def _ladder_efforts(values: tuple[str, ...]) -> tuple[str, ...]:
    ordered = [item for item in EFFORT_LADDER if item in set(values)]
    return tuple(ordered) if ordered else ()


def _codex_entries_from_cache(
    path: Path,
    warnings: list[str],
) -> tuple[ModelEntry, ...]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        warnings.append(f"ignored unreadable codex model cache {path}: {exc}")
        return ()
    raw_models = value.get("models") if isinstance(value, dict) else None
    if not isinstance(raw_models, list):
        warnings.append(f"codex model cache has no model list: {path}")
        return ()
    entries: list[ModelEntry] = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        levels = item.get("supported_reasoning_levels")
        efforts: list[str] = []
        if isinstance(levels, list):
            for level in levels:
                if isinstance(level, dict) and isinstance(
                    level.get("effort"),
                    str,
                ):
                    efforts.append(level["effort"])
                elif isinstance(level, str):
                    efforts.append(level)
        resolved_efforts = _ladder_efforts(tuple(efforts)) or CODEX_EFFORTS
        default_effort = item.get("default_reasoning_level")
        segment = slug.rsplit("-", 1)[-1]
        aliases = (segment,) if segment and segment != slug else ()
        entries.append(
            ModelEntry(
                canonical=slug,
                aliases=aliases,
                efforts=resolved_efforts,
                default_effort=(
                    default_effort
                    if isinstance(default_effort, str)
                    and default_effort in resolved_efforts
                    else None
                ),
                hidden=item.get("visibility") == "hide",
            )
        )
    return _dedupe_aliases(tuple(entries))


def _codex_configured_model(path: Path) -> str | None:
    try:
        import tomllib
    except ImportError:
        return None
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    model = value.get("model")
    return model if isinstance(model, str) and model else None


def _entry_from_override(
    value: object,
    provider: AgentProvider,
    warnings: list[str],
) -> ModelEntry | None:
    if not isinstance(value, dict):
        warnings.append(f"catalog override entry for {provider.value} is not an object")
        return None
    canonical = value.get("canonical")
    if not isinstance(canonical, str) or not canonical:
        warnings.append(f"catalog override entry for {provider.value} has no canonical")
        return None
    raw_aliases = value.get("aliases", [])
    aliases = tuple(
        item
        for item in (raw_aliases if isinstance(raw_aliases, list) else [])
        if isinstance(item, str) and item
    )
    raw_efforts = value.get("efforts", [])
    efforts = _ladder_efforts(
        tuple(
            item
            for item in (raw_efforts if isinstance(raw_efforts, list) else [])
            if isinstance(item, str)
        )
    )
    if not efforts:
        efforts = (
            CLAUDE_EFFORTS
            if provider is AgentProvider.CLAUDE
            else CODEX_EFFORTS
        )
    default_effort = value.get("default_effort")
    return ModelEntry(
        canonical=canonical,
        aliases=aliases,
        efforts=efforts,
        default_effort=(
            default_effort
            if isinstance(default_effort, str) and default_effort in efforts
            else None
        ),
        hidden=bool(value.get("hidden", False)),
    )


def _apply_override(
    path: Path,
    models: dict[AgentProvider, tuple[ModelEntry, ...]],
    defaults: dict[AgentProvider, str],
    warnings: list[str],
) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        warnings.append(f"ignored invalid {CATALOG_FILENAME}: {exc}")
        return False
    providers = value.get("providers") if isinstance(value, dict) else None
    if not isinstance(providers, dict):
        warnings.append(f"ignored {CATALOG_FILENAME}: no providers object")
        return False
    applied = False
    for provider in AgentProvider:
        section = providers.get(provider.value)
        if not isinstance(section, dict):
            continue
        raw_models = section.get("models")
        if isinstance(raw_models, list):
            entries = tuple(
                entry
                for entry in (
                    _entry_from_override(item, provider, warnings)
                    for item in raw_models
                )
                if entry is not None
            )
            if entries:
                models[provider] = _dedupe_aliases(entries)
                applied = True
        default_model = section.get("default_model")
        if isinstance(default_model, str) and default_model:
            defaults[provider] = default_model
            applied = True
    return applied


def load_catalog(
    harness_root: Path,
    *,
    home: Path | None = None,
) -> AgentCatalog:
    """Build the catalog from override file, codex cache, then static values."""
    warnings: list[str] = []
    sources: list[str] = []
    home_dir = Path.home() if home is None else Path(home)

    models: dict[AgentProvider, tuple[ModelEntry, ...]] = {
        AgentProvider.CLAUDE: _dedupe_aliases(STATIC_CLAUDE_MODELS),
        AgentProvider.CODEX: _dedupe_aliases(STATIC_CODEX_MODELS),
    }
    defaults = dict(STATIC_DEFAULT_MODEL)
    sources.append("static")

    cache_path = home_dir / CODEX_MODEL_CACHE
    if cache_path.is_file():
        cached = _codex_entries_from_cache(cache_path, warnings)
        if cached:
            models[AgentProvider.CODEX] = cached
            sources.append(str(cache_path))

    config_path = home_dir / CODEX_CONFIG
    if config_path.is_file():
        configured = _codex_configured_model(config_path)
        if configured is not None and any(
            item.canonical == configured
            for item in models[AgentProvider.CODEX]
        ):
            defaults[AgentProvider.CODEX] = configured
            sources.append(str(config_path))

    override_path = Path(harness_root) / CATALOG_FILENAME
    if override_path.is_file():
        if _apply_override(override_path, models, defaults, warnings):
            sources.append(str(override_path))

    for provider in AgentProvider:
        names = {item.canonical for item in models.get(provider, ())}
        if defaults.get(provider) not in names:
            visible = [
                item.canonical
                for item in models.get(provider, ())
                if not item.hidden
            ]
            fallback = (
                visible[0]
                if visible
                else (
                    models[provider][0].canonical
                    if models.get(provider)
                    else STATIC_DEFAULT_MODEL[provider]
                )
            )
            if defaults.get(provider) is not None:
                warnings.append(
                    f"catalog default model for {provider.value} is unknown; "
                    f"using {fallback!r}"
                )
            defaults[provider] = fallback

    return AgentCatalog(
        models=models,
        default_model=defaults,
        sources=tuple(sources),
        warnings=tuple(warnings),
    )


def _canonical_index(
    entries: tuple[ModelEntry, ...],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    canonical = {item.canonical: item.canonical for item in entries}
    alias: dict[str, str] = {}
    normalized: dict[str, str] = {}
    for item in entries:
        normalized.setdefault(normalize_token(item.canonical), item.canonical)
        for value in item.aliases:
            alias.setdefault(value, item.canonical)
            normalized.setdefault(normalize_token(value), item.canonical)
    return canonical, alias, normalized


def resolve_model(
    catalog: AgentCatalog,
    provider: AgentProvider,
    requested: str | None,
    *,
    strict: bool = False,
) -> ResolvedValue:
    if requested is None or not requested.strip():
        return ResolvedValue(requested, None, METHOD_INHERIT)
    value = requested.strip()
    entries = catalog.entries(provider)
    if not entries:
        return ResolvedValue(requested, value, METHOD_EXACT)
    canonical, alias, normalized = _canonical_index(entries)
    if value in canonical:
        return ResolvedValue(requested, value, METHOD_EXACT)
    if value in alias:
        return ResolvedValue(requested, alias[value], METHOD_ALIAS)
    key = normalize_token(value)
    if key in normalized:
        return ResolvedValue(requested, normalized[key], METHOD_NORMALIZED)

    hidden = {item.canonical for item in entries if item.hidden}
    candidates = [
        item for item, target in normalized.items() if target not in hidden
    ]
    match = difflib.get_close_matches(
        key,
        candidates,
        n=1,
        cutoff=FUZZY_CUTOFF,
    )
    known = ", ".join(catalog.model_names(provider))
    if match:
        target = normalized[match[0]]
        if strict:
            raise UnknownAgentValueError(
                f"model {requested!r} is not a {provider.value} catalog value; "
                f"closest match is {target!r}"
            )
        return ResolvedValue(
            requested,
            target,
            METHOD_FUZZY,
            f"model {requested!r} is not in the {provider.value} catalog; "
            f"using closest match {target!r}",
        )
    if strict:
        raise UnknownAgentValueError(
            f"model {requested!r} is not a {provider.value} catalog value; "
            f"known models: {known}"
        )
    fallback = catalog.default_model[provider]
    return ResolvedValue(
        requested,
        fallback,
        METHOD_DEFAULT,
        f"model {requested!r} is not in the {provider.value} catalog; "
        f"using default {fallback!r} (known models: {known})",
    )


def _ladder_index(value: str) -> int:
    return EFFORT_LADDER.index(value)


def _supported_efforts(
    catalog: AgentCatalog,
    provider: AgentProvider,
    model_value: str | None,
) -> tuple[str, ...]:
    entry = catalog.entry(provider, model_value)
    if entry is not None and entry.efforts:
        return entry.efforts
    return (
        CLAUDE_EFFORTS
        if provider is AgentProvider.CLAUDE
        else CODEX_EFFORTS
    )


def resolve_effort(
    catalog: AgentCatalog,
    provider: AgentProvider,
    model_value: str | None,
    requested: str | None,
    *,
    strict: bool = False,
) -> ResolvedValue:
    if requested is None or not requested.strip():
        return ResolvedValue(requested, None, METHOD_INHERIT)
    value = requested.strip()
    supported = _supported_efforts(catalog, provider, model_value)
    if value in supported:
        return ResolvedValue(requested, value, METHOD_EXACT)

    key = normalize_token(value)
    ladder_by_key = {normalize_token(item): item for item in EFFORT_LADDER}
    candidate: str | None = None
    method = METHOD_EXACT
    if key in EFFORT_ALIASES:
        candidate = EFFORT_ALIASES[key]
        method = METHOD_ALIAS
    elif key in ladder_by_key:
        candidate = ladder_by_key[key]
        method = METHOD_NORMALIZED
    else:
        match = difflib.get_close_matches(
            key,
            list(ladder_by_key) + list(EFFORT_ALIASES),
            n=1,
            cutoff=FUZZY_CUTOFF,
        )
        if match:
            candidate = ladder_by_key.get(match[0]) or EFFORT_ALIASES[match[0]]
            method = METHOD_FUZZY

    known = ", ".join(supported)
    if candidate is None:
        if strict:
            raise UnknownAgentValueError(
                f"effort {requested!r} is not a known level; "
                f"supported: {known}"
            )
        entry = catalog.entry(provider, model_value)
        fallback = (
            entry.default_effort
            if entry is not None and entry.default_effort
            else supported[min(1, len(supported) - 1)]
        )
        return ResolvedValue(
            requested,
            fallback,
            METHOD_DEFAULT,
            f"effort {requested!r} is not a known level; using {fallback!r} "
            f"(supported: {known})",
        )
    if candidate in supported:
        if strict and method is METHOD_FUZZY:
            raise UnknownAgentValueError(
                f"effort {requested!r} is not an exact level; "
                f"closest match is {candidate!r}"
            )
        warning = (
            f"effort {requested!r} resolved to {candidate!r}"
            if method == METHOD_FUZZY
            else None
        )
        return ResolvedValue(requested, candidate, method, warning)

    lowered = [
        item
        for item in supported
        if _ladder_index(item) <= _ladder_index(candidate)
    ]
    clamped = lowered[-1] if lowered else supported[0]
    if strict:
        raise UnknownAgentValueError(
            f"model {model_value!r} does not support effort {candidate!r}; "
            f"supported: {known}"
        )
    return ResolvedValue(
        requested,
        clamped,
        METHOD_CLAMPED,
        f"model {model_value!r} does not support effort {candidate!r}; "
        f"using {clamped!r} (supported: {known})",
    )


def resolve_agent(
    catalog: AgentCatalog,
    provider: AgentProvider,
    model: str | None,
    effort: str | None,
    *,
    strict: bool = False,
) -> ResolvedAgent:
    resolved_model = resolve_model(catalog, provider, model, strict=strict)
    resolved_effort = resolve_effort(
        catalog,
        provider,
        resolved_model.value,
        effort,
        strict=strict,
    )
    return ResolvedAgent(provider, resolved_model, resolved_effort)


def describe_value(value: ResolvedValue) -> str:
    shown = value.value if value.value is not None else "<provider-default>"
    if value.method == METHOD_INHERIT:
        return shown
    if value.requested == value.value:
        return shown
    return f"{shown} (from {value.requested!r}, {value.method})"


def describe_catalog(catalog: AgentCatalog) -> tuple[str, ...]:
    lines = [f"catalog sources: {', '.join(catalog.sources)}"]
    for provider in AgentProvider:
        names = ", ".join(
            item.canonical
            for item in catalog.entries(provider)
            if not item.hidden
        )
        lines.append(
            f"- {provider.value}: default={catalog.default_model[provider]}; "
            f"models={names}"
        )
    lines.extend(f"[WARN] {item}" for item in catalog.warnings)
    return tuple(lines)
