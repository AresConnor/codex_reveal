# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-17

First stable release of **Reveal** — a Textual TUI for Codex Desktop logs and live Responses API SSE.

### Added

- Real `ResponseCard` Live feed: one card per Responses API response, semantic items with Text/Raw expand.
- Pure `ResponseRouter` with generation-family routing and honest attribution (`confirmed` / `inferred` / `unassigned`).
- Live scope filter:
  - Workspace / Session / no selection → **total** Live feed (including unassigned)
  - Agent / subagent → filter that thread
  - Unassigned → unassigned only
- Process→thread breadcrumb attribution for concurrent subagents (unique recent thread only; never guess under ambiguity).
- Multi-part reasoning summaries (`summary_index`) with encrypted-body notice when present.
- Virtual large-content view + fullscreen search for big raw/tool payloads.
- History agent view with `tokens: in=… cached=… out=… cached_rate=…%`.
- Diagnostics tab with whitespace/token anomaly signals and routing metrics.
- Bounded SQLite backfill + high-water live poll (read-only).
- Rollout-derived tool results joined on exact `call_id`.

### Fixed

- Live UI freeze after long runs: cap `unresolved` queue, retry once per batch, O(1) family index only.
- `DuplicateIds` when remounting cards during scope/filter clicks — reuse card widgets, no fixed DOM ids.
- `VirtualContentView.render_line` returns Textual `Strip` (no more `adjust_cell_length` crash).
- History no longer re-parses multi-MB JSONL every poll when the file is unchanged.
- Diagnostics still fills while the tab is hidden (width guard removed).

### Notes

- No Windows `.exe` artifact in this release; install with `uv` / Python ≥ 3.13.
- Full model reasoning bodies remain provider-encrypted; Text mode shows plaintext summaries only.

## [0.1.0] - 2026-07-15

Initial prototype: RichLog Live SSE table, session tree, diagnostics experiments.
