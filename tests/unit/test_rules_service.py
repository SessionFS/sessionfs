"""Unit tests for the rules service (context splitter + etag)."""

from __future__ import annotations

from sessionfs.server.services.rules import (
    DEFAULT_CONTEXT_SECTIONS,
    DEFAULT_KNOWLEDGE_TYPES,
    split_context_sections,
)


def test_split_context_sections_basic():
    doc = """# Project Context

## Overview
It's a tool.

## Architecture
FastAPI + PG.

## Conventions
snake_case.
"""
    sections = split_context_sections(doc)
    assert set(sections.keys()) == {"overview", "architecture", "conventions"}
    assert "FastAPI" in sections["architecture"]


def test_split_skips_empty_html_only_sections():
    doc = """## Overview
<!-- What is this project? -->

## Architecture
Real content here.
"""
    sections = split_context_sections(doc)
    assert "overview" not in sections
    assert "architecture" in sections


def test_split_empty_document():
    assert split_context_sections("") == {}
    assert split_context_sections("no headings here") == {}


def test_defaults_match_design_rules():
    # Key design rule #5: only convention + decision are injected by default
    assert set(DEFAULT_KNOWLEDGE_TYPES) == {"convention", "decision"}
    assert set(DEFAULT_CONTEXT_SECTIONS) == {"overview", "architecture"}
