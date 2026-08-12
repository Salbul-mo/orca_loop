from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orca_loop.catalog import (
    METHOD_ALIAS,
    METHOD_CLAMPED,
    METHOD_DEFAULT,
    METHOD_EXACT,
    METHOD_FUZZY,
    METHOD_INHERIT,
    METHOD_NORMALIZED,
    UnknownAgentValueError,
    describe_catalog,
    load_catalog,
    resolve_agent,
    resolve_effort,
    resolve_model,
)
from orca_loop.models import AgentProvider


CACHE_VALUE = {
    "fetched_at": "2026-08-11T07:12:02Z",
    "models": [
        {
            "slug": "gpt-5.6-sol",
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [
                {"effort": "low"},
                {"effort": "medium"},
                {"effort": "high"},
                {"effort": "xhigh"},
                {"effort": "max"},
                {"effort": "ultra"},
            ],
            "visibility": "list",
        },
        {
            "slug": "gpt-5.6-terra",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [
                {"effort": "low"},
                {"effort": "medium"},
                {"effort": "high"},
                {"effort": "xhigh"},
                {"effort": "max"},
                {"effort": "ultra"},
            ],
            "visibility": "list",
        },
        {
            "slug": "gpt-5.5",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [
                {"effort": "low"},
                {"effort": "medium"},
                {"effort": "high"},
                {"effort": "xhigh"},
            ],
            "visibility": "list",
        },
        {
            "slug": "codex-auto-review",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [{"effort": "medium"}],
            "visibility": "hide",
        },
    ],
}


class CatalogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.harness = self.root / "harness"
        self.harness.mkdir()
        self.home = self.root / "home"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex" / "models_cache.json").write_text(
            json.dumps(CACHE_VALUE),
            encoding="utf-8",
        )
        self.catalog = load_catalog(self.harness, home=self.home)


class LoadCatalogTest(CatalogTestCase):
    def test_codex_models_come_from_cache(self) -> None:
        names = self.catalog.model_names(AgentProvider.CODEX)
        self.assertIn("gpt-5.6-terra", names)
        self.assertIn("gpt-5.5", names)
        self.assertIn("codex-auto-review", names)

    def test_hidden_models_are_marked(self) -> None:
        entry = self.catalog.entry(AgentProvider.CODEX, "codex-auto-review")
        assert entry is not None
        self.assertTrue(entry.hidden)

    def test_static_fallback_without_cache(self) -> None:
        empty_home = self.root / "empty-home"
        empty_home.mkdir()
        catalog = load_catalog(self.harness, home=empty_home)
        self.assertIn(
            "gpt-5.6-terra",
            catalog.model_names(AgentProvider.CODEX),
        )
        self.assertIn("sonnet", catalog.model_names(AgentProvider.CLAUDE))

    def test_invalid_override_is_ignored_with_warning(self) -> None:
        (self.harness / "agent-catalog.json").write_text(
            "{ not json",
            encoding="utf-8",
        )
        catalog = load_catalog(self.harness, home=self.home)
        self.assertIn(
            "gpt-5.6-terra",
            catalog.model_names(AgentProvider.CODEX),
        )
        self.assertTrue(catalog.warnings)

    def test_override_replaces_provider_models(self) -> None:
        (self.harness / "agent-catalog.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "providers": {
                        "claude": {
                            "default_model": "house-model",
                            "models": [
                                {
                                    "canonical": "house-model",
                                    "aliases": ["house"],
                                    "efforts": ["low", "high"],
                                    "default_effort": "high",
                                }
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        catalog = load_catalog(self.harness, home=self.home)
        self.assertEqual(
            catalog.model_names(AgentProvider.CLAUDE),
            ("house-model",),
        )
        resolved = resolve_model(catalog, AgentProvider.CLAUDE, "house")
        self.assertEqual(resolved.value, "house-model")

    def test_describe_catalog_lists_sources(self) -> None:
        lines = describe_catalog(self.catalog)
        self.assertTrue(any("catalog sources" in item for item in lines))


class ResolveModelTest(CatalogTestCase):
    def test_exact_claude_alias_is_kept(self) -> None:
        resolved = resolve_model(self.catalog, AgentProvider.CLAUDE, "sonnet")
        self.assertEqual(resolved.value, "sonnet")
        self.assertEqual(resolved.method, METHOD_EXACT)
        self.assertIsNone(resolved.warning)

    def test_full_model_name_is_passed_through(self) -> None:
        resolved = resolve_model(
            self.catalog,
            AgentProvider.CLAUDE,
            "claude-sonnet-5",
        )
        self.assertEqual(resolved.value, "claude-sonnet-5")
        self.assertEqual(resolved.method, METHOD_EXACT)

    def test_alias_is_mapped(self) -> None:
        resolved = resolve_model(self.catalog, AgentProvider.CLAUDE, "sonnet5")
        self.assertEqual(resolved.value, "sonnet")
        self.assertEqual(resolved.method, METHOD_ALIAS)

    def test_codex_short_alias_is_mapped(self) -> None:
        resolved = resolve_model(self.catalog, AgentProvider.CODEX, "terra")
        self.assertEqual(resolved.value, "gpt-5.6-terra")
        self.assertEqual(resolved.method, METHOD_ALIAS)

    def test_normalized_spacing_and_case(self) -> None:
        resolved = resolve_model(
            self.catalog,
            AgentProvider.CODEX,
            "GPT 5.6 Terra",
        )
        self.assertEqual(resolved.value, "gpt-5.6-terra")
        self.assertEqual(resolved.method, METHOD_NORMALIZED)

    def test_fuzzy_typo(self) -> None:
        resolved = resolve_model(
            self.catalog,
            AgentProvider.CODEX,
            "gpt-5.6-terr",
        )
        self.assertEqual(resolved.value, "gpt-5.6-terra")
        self.assertEqual(resolved.method, METHOD_FUZZY)
        self.assertIsNotNone(resolved.warning)

    def test_unknown_model_uses_default(self) -> None:
        resolved = resolve_model(
            self.catalog,
            AgentProvider.CODEX,
            "없는모델",
        )
        self.assertEqual(
            resolved.value,
            self.catalog.default_model[AgentProvider.CODEX],
        )
        self.assertEqual(resolved.method, METHOD_DEFAULT)
        self.assertIsNotNone(resolved.warning)

    def test_strict_rejects_unknown_model(self) -> None:
        with self.assertRaises(UnknownAgentValueError):
            resolve_model(
                self.catalog,
                AgentProvider.CODEX,
                "없는모델",
                strict=True,
            )

    def test_strict_rejects_fuzzy_model(self) -> None:
        with self.assertRaises(UnknownAgentValueError):
            resolve_model(
                self.catalog,
                AgentProvider.CODEX,
                "gpt-5.6-terr",
                strict=True,
            )

    def test_none_model_inherits(self) -> None:
        resolved = resolve_model(self.catalog, AgentProvider.CLAUDE, None)
        self.assertIsNone(resolved.value)
        self.assertEqual(resolved.method, METHOD_INHERIT)


class ResolveEffortTest(CatalogTestCase):
    def test_exact_effort(self) -> None:
        resolved = resolve_effort(
            self.catalog,
            AgentProvider.CLAUDE,
            "sonnet",
            "medium",
        )
        self.assertEqual(resolved.value, "medium")
        self.assertEqual(resolved.method, METHOD_EXACT)

    def test_effort_alias(self) -> None:
        resolved = resolve_effort(
            self.catalog,
            AgentProvider.CLAUDE,
            "sonnet",
            "mid",
        )
        self.assertEqual(resolved.value, "medium")
        self.assertEqual(resolved.method, METHOD_ALIAS)

    def test_effort_case_normalization(self) -> None:
        resolved = resolve_effort(
            self.catalog,
            AgentProvider.CLAUDE,
            "sonnet",
            "X-High",
        )
        self.assertEqual(resolved.value, "xhigh")

    def test_unsupported_effort_is_clamped_down(self) -> None:
        resolved = resolve_effort(
            self.catalog,
            AgentProvider.CODEX,
            "gpt-5.5",
            "max",
        )
        self.assertEqual(resolved.value, "xhigh")
        self.assertEqual(resolved.method, METHOD_CLAMPED)
        self.assertIsNotNone(resolved.warning)

    def test_claude_does_not_accept_ultra(self) -> None:
        resolved = resolve_effort(
            self.catalog,
            AgentProvider.CLAUDE,
            "sonnet",
            "ultra",
        )
        self.assertEqual(resolved.value, "max")
        self.assertEqual(resolved.method, METHOD_CLAMPED)

    def test_unknown_effort_uses_model_default(self) -> None:
        resolved = resolve_effort(
            self.catalog,
            AgentProvider.CODEX,
            "gpt-5.6-terra",
            "짱쎄게",
        )
        self.assertEqual(resolved.value, "medium")
        self.assertEqual(resolved.method, METHOD_DEFAULT)

    def test_strict_rejects_clamped_effort(self) -> None:
        with self.assertRaises(UnknownAgentValueError):
            resolve_effort(
                self.catalog,
                AgentProvider.CODEX,
                "gpt-5.5",
                "max",
                strict=True,
            )

    def test_none_effort_inherits(self) -> None:
        resolved = resolve_effort(
            self.catalog,
            AgentProvider.CODEX,
            "gpt-5.6-terra",
            None,
        )
        self.assertIsNone(resolved.value)
        self.assertEqual(resolved.method, METHOD_INHERIT)


class ResolveAgentTest(CatalogTestCase):
    def test_provider_is_never_changed(self) -> None:
        for provider in AgentProvider:
            for model in ("sonnet", "terra", "없는모델", None):
                resolved = resolve_agent(
                    self.catalog,
                    provider,
                    model,
                    "medium",
                )
                self.assertIs(resolved.provider, provider)

    def test_effort_follows_resolved_model(self) -> None:
        resolved = resolve_agent(
            self.catalog,
            AgentProvider.CODEX,
            "gpt 5.5",
            "max",
        )
        self.assertEqual(resolved.model.value, "gpt-5.5")
        self.assertEqual(resolved.effort.value, "xhigh")
        self.assertEqual(len(resolved.warnings), 1)


if __name__ == "__main__":
    unittest.main()
