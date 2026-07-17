# Reveal Live SSE Response Cards and Agent Tree Redesign

Date: 2026-07-16

Status: Implemented (2026-07-16) — real ResponseCard widgets, pure router with generation-family routing, session tree scopes, bounded backfill, virtual content

Scope: `D:\workspace\reveal`

## 1. Goal

Replace the current line-oriented Live SSE rendering with a real Textual widget hierarchy that is safe under concurrent root agents and subagents.

The end state must provide:

- One real `ResponseCard` widget for each Responses API response.
- One semantic item row for each response output item, separated visually inside the card.
- Per-item expansion into assembled semantic content and verbatim raw SSE events.
- Correct root/subagent grouping and filtering through the existing left tree.
- Explicit attribution confidence when an SSE stream cannot be mapped to an agent with certainty.
- Bounded, non-blocking behavior on the current multi-gigabyte Codex log database.

This is a debug tool. Fidelity, attribution honesty, stable ordering, and inspectability take precedence over decorative presentation.

## 2. Current Problems

### 2.1 Fake cards

`src/reveal/widgets/log_view.py` currently writes headers and `====` / `----` lines into a `RichLog`. Those lines are not independent widgets and therefore cannot provide proper focus, per-card state, per-item expansion, targeted updates, or virtualized raw content.

### 2.2 Wrong Active Agents identity

The current `_active_agents` mapping uses `response_id` as if it were an agent identity. A response is not an agent. One thread can produce many responses, and multiple agent threads can produce responses concurrently.

### 2.3 Unsafe concurrent attribution

Many SSE delta events do not contain `response_id`. The current fallback assigns an ambiguous event to the only active response when exactly one exists. That is unsafe as soon as root and subagents overlap or two responses are interleaved.

### 2.4 Full redraws

The current render path clears and rebuilds the entire `RichLog` whenever new events arrive. Cost grows with all retained cards and all retained deltas, even if only one response changed.

### 2.5 Left and right navigation duplication

The left tree already contains root and subagent rollout metadata. The separate right-side Active Agents list duplicates navigation space while still using the wrong response-level identity.

### 2.6 Startup loses context

The current SSE source begins at the latest log id. A response already in flight when Reveal starts will be missing `response.created`, and recent completed responses are not available in Live.

### 2.7 Main-thread work

Session scans, rollout reads, and SQLite reads currently run synchronously from Textual callbacks. The prior full-table count was removed, but the broader redesign must establish an explicit no-blocking-I/O rule for the UI thread.

## 3. Confirmed Product Decisions

Every decision in this section was explicitly confirmed during the design interview.

### 3.1 Card boundary

One Responses API response is one `ResponseCard`:

```text
response.created -> zero or more output items -> response.completed/failed/incomplete
```

The response card is not an agent card and not a raw-event card.

### 3.2 Semantic item boundary

Inside a response card, output items are numbered from one in display order:

```text
1  Thinking
------------------------------------------------------------
2  Tool Call: shell_command
------------------------------------------------------------
3  Thinking
------------------------------------------------------------
4  Answer
```

The source identity is `(response_id, output_index, item_id)`. `output_index` is zero-based in the protocol but one-based in the UI.

### 3.3 Raw-event hierarchy

Each semantic item owns its raw SSE children:

```text
2  Tool Call: shell_command
  2.1  response.output_item.added
  2.2  response.custom_tool_call_input.delta
  2.3  response.custom_tool_call_input.delta
  2.4  response.output_item.done
```

Response-wide events that do not belong to an output item remain in a response-level `Other` section rather than being fabricated into a semantic item.

### 3.4 Classification

Use protocol item type first, event family second:

| Protocol signal | UI classification |
| --- | --- |
| `item.type == reasoning` | Thinking |
| `item.type == custom_tool_call` | Tool Call |
| `item.type == function_call` | Tool Call |
| `item.type == message` | Answer |
| Unknown item types | Other |
| Response-wide or non-standard target lines | Other |

Unknown future event types must remain visible and inspectable.

### 3.5 Summary and expansion

- A card is expanded to its summary body by default.
- Each semantic item shows a one-line summary while collapsed.
- Each semantic item can independently expand to full assembled semantic content and its raw SSE children.
- The card header provides Expand All and Collapse All for raw item details.
- The whole card can be collapsed separately to a metadata-only header.
- User expansion state must survive live updates.
- A new event must never forcibly reopen a card or item that the user closed.

### 3.6 Raw fidelity

Raw display contains:

1. A Reveal-generated scan header with timestamp, sequence number, log id, and event type.
2. The database `feedback_log_body` verbatim.

Reveal must not re-serialize JSON, change field order, normalize escaping, or pretty-print the raw body. Parsed data is allowed only in the summary layer.

Non-standard rows such as `unhandled responses event: ...` are retained in `Other`.

### 3.7 Tool execution results

Tool Call summaries combine two explicitly labeled sources:

- Responses SSE: tool name, call id, generated arguments/input, item lifecycle.
- Rollout JSONL: execution status, duration when available, exit/result summary, and output.

Join the two sources only through an exact `call_id`. If no result exists, show `running` or `result unavailable`; never infer success.

Raw SSE remains pure SSE. Rollout tool output is displayed in a separate semantic result subsection.

### 3.8 Large content

- Small content expands inline.
- Content over 200 lines or 64 KiB uses a virtual view.
- The embedded virtual view is approximately 18 terminal rows high.
- The virtual view reports visible and total ranges.
- A full-screen view uses the same data source and preserves scroll position on return.
- Full-screen search supports plain text by default, optional regex, next/previous match, match count, and highlighting.
- Full data is retained; virtualization is a rendering strategy, not truncation.

### 3.9 Left tree replaces Active Agents

Remove the right-side Active Agents pane. Rebuild the left tree as:

```text
Workspace
  Root session: time and status
    root
    subagent nickname and role
```

Example:

```text
MCServerLauncher-Future
  01:14 active
    root
    Gibbs       worker
    Popper      explorer
  Yesterday 17:12 closed
    root
    Einstein    default
```

Do not prefix workspace labels with `Root -` or `Root .`.

### 3.10 Tree selection semantics

- Workspace selects all root sessions under that workspace.
- Root session selects its root agent and descendants.
- Agent selects only that thread.
- Selection changes the current scope but does not switch tabs.
- In Live, selection filters response cards.
- In History, selecting an agent loads its rollout.
- History does not concatenate every agent when a workspace or session node is selected; it asks the user to select an agent.

### 3.11 Tree order and retention

- Agent identity is the Codex thread id.
- Agents retain stable spawn/first-seen order.
- Closed agents stay visible, become dim, and remain selectable.
- Active sessions are always shown.
- Each workspace initially shows its 20 most recent completed sessions.
- `Load older...` adds 20 more sessions for that workspace.
- Pagination changes only the UI catalog; it never deletes rollout files or cached response data.

### 3.12 Auto-follow behavior

- Cards are ordered by response creation/log order, oldest first and newest at the bottom.
- If the viewport is already at the bottom, new cards and updates keep it pinned to the bottom.
- If the user scrolls upward, auto-follow pauses.
- While paused, show counts of new events and updated responses.
- Clicking the indicator or pressing End returns to the bottom and resumes following.
- Expanding an older item must not pull the viewport to the bottom.

### 3.13 Startup backfill

- Backfill the most recent 20 completed responses.
- Reconstruct current in-flight responses when possible.
- Mark a response `partial` when its beginning cannot be found within the bounded backfill.
- Capture a SQLite high-water mark so events arriving during backfill are not skipped or duplicated.
- Do not scan the entire log database.
- `Ctrl+R` refreshes the catalog and retries attribution without clearing loaded cards.

### 3.14 Attribution confidence

Every response has one of three attribution states:

| State | Meaning | UI behavior |
| --- | --- | --- |
| `confirmed` | Exact thread/rollout/call evidence | No warning badge required |
| `inferred` | Deterministic but indirect correlation | Yellow `inferred` badge |
| `unassigned` | No safe agent assignment | Top-level Unassigned scope and gray badge |

Allowed transitions:

```text
unassigned -> inferred -> confirmed
unassigned -> confirmed
```

Confidence never downgrades. A response must not oscillate between agents. Any conflict after a confirmed assignment is retained as diagnostic evidence and must not silently move the card.

### 3.15 Unassigned behavior

- Show an unassigned response immediately; do not hide or drop it.
- Continue buffering and rendering its events.
- Move it atomically when stronger attribution arrives.
- Preserve response order, item order, raw order, focus where possible, and expansion state.
- A completed unresolved response remains under Unassigned.
- Never assign based only on `process_uuid` or "the only active response".

## 4. Official Codex Principles to Reuse

Reference checkout used during planning:

- Repository: `openai/codex`
- Commit: `38b064c31b1f7464b281006316ec878ed23fea77`
- Commit date: 2026-07-15

Relevant official implementation references:

- `codex-rs/tui/src/app/agent_navigation.rs`
- `codex-rs/tui/src/app/thread_events.rs`
- `codex-rs/tui/src/app/thread_routing.rs`
- `codex-rs/tui/src/history_cell/messages.rs`
- `codex-rs/tui/src/history_cell/session.rs`

Reuse these design rules, not Rust code:

1. `ThreadId` is the stable identity for an agent thread.
2. First-seen/spawn order is stable even when metadata and running state change.
3. Closed agents remain inspectable rather than disappearing.
4. Each thread owns independent event state.
5. Streaming content is consolidated into source-backed semantic cells after completion.
6. Borders and width calculations are centralized rather than manually repeated in every renderer.
7. Live rendering follows the bottom only when the user was already at the bottom.

The official Codex manual helper returned HTTP 403 during planning. The official `openaiDeveloperDocs` MCP was added globally, but a new Codex thread/restart is required before that tool becomes available. The source checkout above is therefore the implementation evidence used by this plan.

## 5. Proposed Architecture

### 5.1 Module layout

Keep ownership explicit and avoid another monolithic `log_view.py`:

```text
src/reveal/
  app.py
  models.py
  diagnostics.py
  routing.py                         # new: response and attribution state machine
  sources/
    log_stream.py                    # new: multi-target SQLite high-water polling
    rollout.py                       # catalog, lazy session pages, incremental tailing
    sse.py                           # parser helpers or compatibility facade
  widgets/
    log_view.py                      # tab coordinator only
    session_tree.py                  # workspace/session/agent scope tree
    response_feed.py                 # card collection, filtering, follow state
    response_card.py                 # card and semantic item widgets
    virtual_content.py               # raw/tool virtual ScrollView
    content_screen.py                # full-screen viewer and search
```

Do not add an abstraction solely to match this file list. If two adjacent small modules are clearer together, merge them while retaining the ownership boundaries.

### 5.2 Domain models

Replace nested anonymous dictionaries with typed models.

Suggested types:

```python
class AttributionConfidence(Enum):
    UNASSIGNED = 0
    INFERRED = 1
    CONFIRMED = 2

class ResponseStatus(Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"

class ItemKind(Enum):
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    ANSWER = "answer"
    OTHER = "other"

@dataclass(frozen=True)
class AgentKey:
    thread_id: str

@dataclass
class RawLogEvent:
    log_id: int
    timestamp: float
    target: str
    process_uuid: str | None
    thread_id: str | None
    event_type: str
    sequence_number: int | None
    response_id: str | None
    item_id: str | None
    output_index: int | None
    raw_body: str
    parsed: dict

@dataclass
class ResponseItemState:
    item_id: str
    output_index: int
    kind: ItemKind
    protocol_type: str
    tool_name: str | None
    call_id: str | None
    events: list[RawLogEvent]
    assembled_text: str
    tool_input: str
    tool_result: ToolResult | None

@dataclass
class ResponseState:
    response_id: str
    agent_thread_id: str | None
    attribution: AttributionEvidence
    status: ResponseStatus
    model: str
    reasoning_effort: str | None
    started_at: float
    ended_at: float | None
    usage: dict
    items: OrderedDict[tuple[int, str], ResponseItemState]
    other_events: list[RawLogEvent]
    partial: bool
```

UI state does not belong in these source models. Card collapsed state, item expansion state, focus, search, and scroll offsets stay in widget/view state keyed by stable ids.

### 5.3 Session catalog

Build a catalog indexed by:

```text
thread_id -> SessionMeta
root session id -> ordered agent threads
workspace canonical path -> ordered root sessions
call_id -> rollout tool invocation/result reference
```

Extend `SessionMeta` with an explicit `thread_id` instead of overloading `rollout_id` or `session_id`.

Workspace grouping rules:

- Canonicalize Windows path separators and case for identity.
- Preserve the original path for display and file links.
- Display the final path component as the primary label.
- If two canonical paths share the same basename, include a dim parent-path suffix to disambiguate.

Session identity rules:

- Root rollout: `thread_id == session_id` or no parent thread.
- Subagent rollout: `thread_source == subagent` and `parent_thread_id` is present.
- `session_id` is the root family id already shared by observed subagent rollouts.
- Use thread spawn depth/path for nested subagents; do not assume depth is always one.

### 5.4 Log source

Replace the SSE-only query with a bounded multi-target log stream. Required rows include:

- `codex_api::sse::responses`
- Thread-bound request/client rows needed for response-to-thread correlation.
- Minimal lifecycle rows needed to establish request start/completion evidence.

The query remains row-id based:

```sql
SELECT id, ts, ts_nanos, level, target, feedback_log_body,
       module_path, thread_id, process_uuid
FROM logs
WHERE id > ?
  AND target IN (...)
ORDER BY id ASC
LIMIT ?
```

Requirements:

- No `COUNT(*) WHERE target=...` query.
- No unbounded `fetchall()`.
- Open in read-only URI mode when possible.
- Use a short busy timeout and treat transient lock errors as a retryable poll miss.
- Advance the raw log cursor for every fetched relevant row, even if parsing fails.
- Preserve malformed and non-SSE bodies as raw Other events when their target is relevant.
- Execute SQLite work in a Textual worker/thread, never in the message loop.

### 5.5 Startup high-water algorithm

Backfill must not race with live polling:

1. Open a read-only connection.
2. Capture `high_water_id = MAX(id)` using the primary key fast path.
3. Scan backward from the high-water mark in bounded row-id pages.
4. Stop after finding 20 complete response starts plus all potentially in-flight starts in the scanned window.
5. Enforce a defensive maximum scanned-row budget.
6. Re-read the selected id range in ascending order with all relevant targets.
7. Route those rows through the same parser/state machine used for live events.
8. Mark responses missing a start event as partial.
9. Set the live cursor to `high_water_id` only after backfill state is installed.
10. Poll `id > high_water_id`; this captures rows written during or after reconstruction exactly once.

If the defensive budget is reached, expose a startup diagnostic and keep the partial cards. Do not continue into a multi-gigabyte scan.

### 5.6 Response routing

All routing happens before widgets are touched.

#### Direct response evidence

Use direct response ids from:

- `response.created.response.id`
- `response.completed.response.id`
- `response.failed.response.id`
- `response.incomplete.response.id`
- Explicit top-level `response_id` when present.

#### Output item evidence

Register each `response.output_item.added` / `done` item by `item_id` and `output_index`.

Observed Codex ids share a response-generation prefix, but that format is not a documented public contract. Prefix correlation may contribute inferred evidence only; it must never produce confirmed attribution by itself.

#### Sequence evidence

Track sequence numbers per response stream. Sequence continuity can reject impossible candidates but cannot uniquely select a response when concurrent streams have the same next sequence. It is supporting evidence, not a sole identity key.

#### Concurrent ambiguity

When more than one candidate remains:

- Keep the event in an unresolved queue keyed by process, item id, and log interval.
- Retry after later item/response lifecycle evidence arrives.
- If still unresolved, attach it to an Unassigned response stream rather than choosing an active response.

Remove the current single-active-response fallback completely.

### 5.7 Agent attribution

Maintain an `AttributionEvidence` object containing:

- chosen thread id, if any;
- confidence;
- evidence kind;
- source log ids;
- conflict details;
- update timestamp.

Evidence precedence from strongest to weakest:

1. Exact rollout/call relationship with known thread id.
2. Exact thread-bound request lifecycle correlated to the response creation on the same request/process path.
3. Deterministic item/response stream correlation combined with one thread-bound request candidate.
4. Opaque id-prefix and time adjacency heuristics.
5. Process-only or single-active guesses, which are forbidden.

Only levels 1-2 are `confirmed`. Levels 3-4 are `inferred` and must display the badge.

When a stronger assignment arrives:

- update the model;
- notify the tree/feed coordinator;
- reparent or remount the card without recreating its state;
- keep its global order key;
- preserve focus and expansion state if Textual permits; otherwise restore focus immediately after remount.

### 5.8 Tool lifecycle join

Tail rollout JSONL incrementally by byte offset rather than rereading the entire file every 500 ms.

Index:

```text
call_id -> invocation record
call_id -> output record
```

On a matching Tool Call item:

- attach the invocation immediately;
- update to running/completed/failed when output arrives;
- keep output as a lazy source reference for large content;
- notify only the owning semantic item widget.

Rollout rotation/truncation must reset the file offset safely. Invalid trailing JSON during an active write is retried on the next tail pass.

### 5.9 Diagnostics integration

Keep the current `ResponseAnalyzer`, but feed it from routed `RawLogEvent` objects before view filtering. Diagnostics must continue to observe hidden agents and hidden tabs.

Add routing diagnostics:

- confirmed/inferred/unassigned response counts;
- unresolved event queue size;
- attribution conflicts;
- partial backfill count;
- current SQLite lag measured as latest relevant id minus consumed id when cheaply available.

Do not perform a full-table query to compute diagnostics.

## 6. Widget Design

### 6.1 Live layout

```text
Horizontal
  SessionTree (left, resizable/fixed responsive width)
  LogView
    TabbedContent
      Live: ResponseFeed
      History: existing history view
      Diagnostics: existing diagnostics view
```

Delete `#agent-sidebar`, `#agent-title`, and `#agent-filter` from composition and CSS.

### 6.2 ResponseFeed

`ResponseFeed` owns:

- ordered response ids;
- currently mounted card widgets;
- current scope filter;
- card retention limit;
- follow-bottom state;
- counts accumulated while follow is paused;
- incremental add/update/remove operations.

It must not clear and replay the whole feed for a single event. Only changed cards update.

Filtering choices:

- Hide/show or mount/unmount based on measured performance.
- Preserve per-card view state in a separate state map when a filtered card is unmounted.
- Apply filters in one batched UI mutation to avoid layout thrash.

### 6.3 ResponseCard

Use a real `Container` or `Vertical` with a Textual CSS border on all sides.

Header fields:

- card collapse icon;
- agent name/path;
- attribution badge when inferred/unassigned;
- model and reasoning effort;
- response status;
- partial badge;
- elapsed/final duration;
- token usage;
- semantic item count;
- Expand All / Collapse All raw control;
- full response id available in detail/tooltip, short id in compact display.

The card body contains semantic item widgets in protocol order. Use a real divider widget or item border between siblings; do not draw fake repeated `=` card borders in a log.

Status styling:

- active: restrained warning/accent color;
- completed: success color;
- failed/incomplete: error color;
- inferred: yellow badge;
- unassigned/partial: dim neutral badge with warning detail.

### 6.4 ResponseItem

Each item is focusable and owns:

- one-line summary row;
- expansion state;
- assembled semantic content;
- optional tool execution subsection;
- raw-event virtual source;
- item-local full-screen action.

One-line summary guidance:

- Thinking: final visible reasoning summary; otherwise type, elapsed time, and event count.
- Tool Call: tool name plus compact argument preview and execution status.
- Answer: first meaningful assembled line, normalized for one-line display.
- Other: protocol type and event-type counts.

Expanded content order:

1. Full assembled semantic content.
2. Tool execution result when applicable, labeled as rollout-derived.
3. Raw SSE event list, labeled as SSE-derived.

### 6.5 Virtual content

Implement a custom `ScrollView`, not `ListView`:

- `ListView` mounts one widget per item and does not satisfy the virtualization goal.
- `OptionList` maintains option and wrapped-line caches for the full option set; it is not ideal for huge multiline raw bodies.
- A custom `ScrollView.render_line(y)` can render only the requested visible line.

`VirtualContentSource` responsibilities:

- immutable logical rows or append-only row updates;
- map virtual display line to source row and subline;
- lazy wrapping by current width;
- bounded LRU cache of rendered/wrapped visible rows;
- match index for search;
- stable logical cursor independent of terminal reflow.

Large-content threshold:

```text
line_count > 200 OR utf8_bytes > 65536
```

Below the threshold, inline content is acceptable. Above it, mount an 18-row virtual viewport.

### 6.6 Full-screen content screen

Use a `Screen` or `ModalScreen` containing the same virtual content source.

Required behavior:

- retain source position when opening and closing;
- `/` opens search;
- normal text search is case-insensitive by default;
- regex is opt-in;
- next/previous match navigation;
- visible match count;
- highlight only visible matches during render;
- invalid regex reports inline without crashing;
- Esc closes search first, then closes the screen;
- no duplicate full-content copy.

### 6.7 Focus and input

Mouse and keyboard must both work.

Minimum keyboard behavior:

- Tree arrows navigate nodes; Enter selects scope.
- Tab/Shift+Tab moves between major focus regions.
- Enter or Space toggles the focused card/item control.
- Left collapses and Right expands the focused card/item where applicable.
- End restores follow-bottom.
- Full-screen and search bindings are scoped to the focused virtual content.

Keep bindings contextual. Avoid global keys that fire while a search input is focused.

## 7. Left Tree Design

### 7.1 Node data

Do not overload `TreeNode.data` with only `SessionMeta`. Use explicit scope data:

```python
@dataclass(frozen=True)
class WorkspaceScope:
    workspace_key: str

@dataclass(frozen=True)
class SessionScope:
    session_id: str

@dataclass(frozen=True)
class AgentScope:
    thread_id: str

@dataclass(frozen=True)
class LoadOlderScope:
    workspace_key: str
```

Post one `ScopeSelected` message with the selected scope. `LogView` decides how that scope affects each tab.

### 7.2 Refresh stability

Refresh must preserve:

- selected scope;
- expanded workspace/session nodes;
- per-workspace loaded page count;
- scroll position where possible;
- stable agent ordering;
- closed status.

Do not `tree.clear()` and blindly rebuild without restoring state.

### 7.3 Live status updates

The session catalog and router notify the tree when:

- a new workspace/session/agent appears;
- an agent transitions active/closed;
- an unassigned response is reassigned;
- a session becomes active/completed.

Batch tree updates to avoid rebuilding for every SSE delta.

## 8. Performance and Concurrency Rules

### 8.1 UI-thread rule

The Textual message loop may perform only:

- small state mutations;
- mounting/unmounting changed widgets;
- visible rendering;
- posting messages.

SQLite, filesystem scanning, JSONL parsing, large search indexing, and large wrap-index computation run in workers.

### 8.2 Poll batching

- Keep bounded SQLite pages, initially 500 relevant rows.
- If a backlog remains, schedule another worker pass without waiting for the normal 500 ms interval.
- Coalesce model updates per response before posting to the UI.
- At most one UI update message per changed response per worker batch.

### 8.3 State isolation

- One `ResponseState` per response id.
- One item map per response.
- One rollout tail state per thread/file.
- One agent state per thread id.
- No module-level mutable singleton state.
- No `current_response` pointer shared across agents.

### 8.4 Retention

Preserve the existing response-card limit presets:

```text
100 / 200 / 500 / 1000
```

Rules:

- Never evict active responses.
- Evict the oldest completed responses first.
- Removing a response also removes stale response routing indexes and virtual caches.
- Session/agent catalog retention is independent from response-card retention.
- If active responses alone exceed the selected limit, temporarily exceed it and expose the condition in diagnostics.

### 8.5 Database safety

- Read-only connections only.
- No schema or index changes to Codex-owned `logs_2.sqlite` in this task.
- No long-lived read transaction across polls.
- No full-table counts or target scans.
- Close connections deterministically.
- Treat database replacement/rotation as a source reset with a new high-water/backfill pass.

## 9. Implementation Sequence

Each phase should be independently testable. Do not begin the next phase with failing tests in the previous phase.

### Phase 0: Baseline and fixtures

1. Preserve the dirty-worktree boundary. Existing `.omc` changes are unrelated and must not be touched.
2. Record current modified files and distinguish prior Live-card edits from new work.
3. Add sanitized fixtures representing:
   - one response with Thinking, Tool Call, and Answer;
   - two interleaved concurrent responses;
   - root and two subagent rollouts;
   - tool invocation/output joined by call id;
   - malformed and non-standard response target lines;
   - a response that starts before the backfill window.
4. Keep fixture bodies small while preserving real field shapes.

Exit criteria:

- Fixtures parse independently.
- Existing three regression tests still pass.

### Phase 1: Typed models and pure router

1. Add enums and dataclasses for raw events, responses, items, attribution, agents, and scopes.
2. Implement pure SSE parsing without Textual imports.
3. Implement semantic classification.
4. Implement response/item routing and unresolved queues.
5. Implement monotonic attribution upgrades.
6. Remove the single-active-response assumption from the new path.

Exit criteria:

- Interleaved fixtures produce two isolated response states.
- Every fixture event is accounted for exactly once.
- Ambiguous events remain unresolved/unassigned.
- Item numbering and classification match the confirmed design.

### Phase 2: Session catalog and rollout tailing

1. Add explicit thread identity to session metadata.
2. Build workspace/session/agent indexes.
3. Preserve nested subagent depth and stable spawn order.
4. Implement per-workspace 20-session pagination.
5. Implement incremental JSONL tailing by byte offset.
6. Build the call-id invocation/output index.

Exit criteria:

- Real observed root/subagent metadata maps to the correct workspace and root session.
- Closed agents remain in order.
- Tool results join only on exact call ids.
- Partial trailing JSON is retried rather than discarded.

### Phase 3: Multi-target source and startup backfill

1. Add row-id-based relevant-target polling.
2. Run database work in a worker thread.
3. Implement high-water startup backfill.
4. Detect database reset/replacement.
5. Feed the pure router and post coalesced model changes.

Exit criteria:

- No missed or duplicate ids across backfill/live handoff.
- Recent 20 complete responses and in-flight responses reconstruct from fixtures.
- Backfill respects its row budget.
- No full-table count query exists.

### Phase 4: Left tree migration

1. Add scope node types.
2. Render workspace/session/agent hierarchy.
3. Add stable state-preserving refresh.
4. Add per-workspace `Load older...`.
5. Stop automatic History tab switching.
6. Emit scope changes to `LogView`.

Exit criteria:

- Workspace, session, and agent filters select the intended thread sets.
- Selection and expansion survive refresh.
- Closed agents are dim and selectable.
- Loading older affects only one workspace.

### Phase 5: Response feed and real cards

1. Remove the Live `RichLog` renderer.
2. Add `ResponseFeed` and incremental card mounting.
3. Add real bordered `ResponseCard` widgets.
4. Add semantic item widgets and dividers.
5. Add card/item expansion state and card-level raw actions.
6. Add attribution, partial, lifecycle, usage, and duration metadata.
7. Remove the right agent pane and its CSS/actions.

Exit criteria:

- A live response appears as one actual card widget.
- Semantic items update in place without a full feed clear.
- Card and item state survive incoming deltas.
- Filtering does not corrupt global card order.

### Phase 6: Tool results and virtual content

1. Attach rollout tool results to matching item widgets.
2. Implement inline small-content rendering.
3. Implement custom virtual raw/tool content view.
4. Add full-screen view and search.
5. Preserve raw-body fidelity.

Exit criteria:

- Large fixture content mounts a bounded number of widgets/visible strips.
- Full-screen search scans complete data.
- Tool output is labeled rollout-derived and raw SSE remains SSE-only.
- Closing/reopening preserves positions.

### Phase 7: Follow behavior and diagnostics

1. Track whether the feed was at bottom before each mutation.
2. Pause follow on manual upward scroll.
3. Add new-event/update indicator and End resume.
4. Extend diagnostics with routing/backfill metrics.
5. Verify hidden tabs ingest without full redraw.

Exit criteria:

- Inspecting an old card is not interrupted by new deltas.
- Bottom-follow remains stable during card height changes.
- Diagnostics observe all routed events regardless of filter.

### Phase 8: Cleanup and compatibility removal

1. Remove obsolete `_cards` dictionary rendering from `LogView`.
2. Remove `_active_agents`, `_filter_rid`, `_render_live`, raw global replay, and right-pane actions.
3. Remove unused CSS and imports.
4. Keep History and Diagnostics behavior unless explicitly superseded above.
5. Update README with the final interaction model and validation command only after behavior is implemented.

Exit criteria:

- No fake `====`/`----` card renderer remains.
- No response id is presented as an agent id.
- No compatibility branch silently routes ambiguous events to an active response.

## 10. Test Plan

### 10.1 Pure model/router tests

Required tests:

1. One response creates one response state.
2. Output indices 0/1/2 display as semantic items 1/2/3.
3. Reasoning, custom tool, function tool, message, and unknown classification.
4. Delta assembly by item id.
5. Raw bodies remain byte-for-byte text-equivalent after read/display sourcing.
6. Two response streams interleave without cross-contamination.
7. Two agents interleave without shared current-response state.
8. Ambiguous event becomes unassigned.
9. Attribution upgrades monotonically.
10. Conflicting confirmed evidence does not move a card.
11. Failed/incomplete/completed lifecycle states.
12. Partial response behavior.

### 10.2 SQLite source tests

Use temporary SQLite databases with the real `logs` column shape.

Required tests:

1. Bounded poll pages.
2. Cursor advances over malformed relevant rows.
3. High-water handoff has no gaps.
4. Rows inserted during backfill are consumed once by live polling.
5. Backfill stops after response target and row-budget criteria.
6. Database replacement resets safely.
7. Busy/lock error retries without blocking the UI path.
8. Query-plan assertion or SQL inspection prevents reintroducing target full-table counts.

### 10.3 Rollout catalog tests

Required tests:

1. Root and subagent grouping by session id.
2. Nested depth/path grouping.
3. Workspace path canonicalization and basename collision labels.
4. Stable spawn ordering.
5. Closed-agent retention.
6. 20-item pagination isolated per workspace.
7. Incremental JSONL append and partial-line retry.
8. Exact call-id tool result join.

### 10.4 Widget tests with Textual pilot

Required tests:

1. `ResponseCard` is mounted as a widget, not rendered as RichLog separators.
2. Card collapse and item expansion are independent.
3. Expand All / Collapse All updates every item.
4. Incoming delta preserves user collapse state.
5. Scope filtering shows the correct cards.
6. Unassigned-to-agent move preserves card state.
7. Tree selection does not switch tabs.
8. Closed tree nodes remain selectable.
9. Auto-follow pauses/resumes correctly.
10. Full-screen content returns to the original scroll position.
11. Search and invalid regex behavior.
12. Narrow and wide terminal layouts do not overlap or clip controls.

### 10.5 Virtualization tests

Instrument the virtual source/view:

- 10,000 logical lines must not create 10,000 child widgets.
- Initial render work must be proportional to viewport height plus bounded cache.
- Scrolling produces correct source line mapping.
- Width changes invalidate only required wrap indexes.
- Search operates over all logical content.

### 10.6 Real environment validation

Use the real Codex database read-only.

Checks:

1. Backfill completes within an explicit timeout on the current approximately 3.46 GB database.
2. Empty live polls remain in the low-millisecond range.
3. A real root plus concurrent subagents produces distinct agent scopes/cards.
4. Attribution counts and evidence details are plausible and inspectable.
5. Headless Textual remains responsive while events stream.
6. Manual TUI pass covers mouse, keyboard, resize, filters, expansion, full-screen search, and follow pause.

Validation commands should include:

```powershell
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests
git diff --check
```

Add targeted performance harness commands rather than relying on subjective visual responsiveness.

## 11. Acceptance Criteria

The redesign is complete only when all statements are true:

1. Live uses real bordered card widgets; no fake `====` card framing remains.
2. One Responses API response maps to exactly one card.
3. Items are separated and numbered by semantic output order.
4. Each item independently expands to full semantic content and numbered raw SSE.
5. Raw body text is verbatim.
6. Tool calls show exact call-id-correlated execution results when available.
7. Root/subagent identity comes from thread/session metadata, never response id.
8. Multiple concurrent agents can update without event mixing.
9. Ambiguous attribution is visible as inferred or unassigned.
10. The left tree provides workspace/session/agent filtering and old-session pagination.
11. Selecting the tree does not force a tab change.
12. Closed agents remain reviewable in stable order.
13. Startup reconstructs recent 20 plus in-flight responses without an unbounded scan.
14. The UI thread performs no SQLite/session scan or large JSON parsing.
15. Only changed card widgets update for new events.
16. Large content is virtualized and searchable.
17. Manual scroll position is respected while live events continue.
18. History and Diagnostics remain functional.
19. All focused tests, compile checks, diff checks, and real-database smoke checks pass.

## 12. Risks and Mitigations

### Risk: SSE events omit response and thread ids

Mitigation: evidence-based routing, unresolved queues, explicit confidence, and Unassigned. Do not conceal uncertainty.

### Risk: Opaque id-prefix behavior changes

Mitigation: prefix correlation is inferred evidence only. Unknown formats remain visible and do not become confirmed assignments.

### Risk: Nested scrolling is awkward

Mitigation: use inline content for small payloads, a clearly focusable 18-row viewport for large payloads, full-screen mode for sustained reading, and retained positions.

### Risk: Card reparenting loses Textual state

Mitigation: keep view state keyed outside the widget instance and restore after remount; prefer changing filter membership without recreating the card when practical.

### Risk: Startup backfill becomes expensive

Mitigation: primary-key high-water scan, bounded reverse pages, defensive row budget, partial badge, worker thread, and performance tests on the real database.

### Risk: Tool outputs are enormous

Mitigation: byte-offset source references, virtual rendering, no duplicate full-content copies, bounded wrap cache, and full-screen reuse of the same source.

### Risk: Session catalog contains thousands of files

Mitigation: worker-based metadata scan, cached file stat/signature data, per-workspace UI pagination, and state-preserving incremental tree updates.

### Risk: Existing dirty work is overwritten

Mitigation: treat current `app.py`, `log_view.py`, `sse.py`, and untracked tests as the baseline. Ignore unrelated `.omc` changes. Review every overlapping diff before replacement.

## 13. Non-Goals

This redesign does not:

- modify Codex or its log database schema;
- add indexes to Codex-owned SQLite files;
- send commands or control agents;
- delete rollout/log history;
- guarantee confirmed attribution when Codex does not emit sufficient identity evidence;
- merge all agents' History JSONL into a synthetic transcript;
- persist UI expansion, search, or filter state across Reveal process restarts in the first implementation;
- replace the existing Diagnostics analyzer beyond the integration changes listed above.

## 14. Delivery and Review Boundaries

Recommended change grouping:

1. Models/router/fixtures.
2. Session catalog/log source/backfill.
3. Tree scopes and pagination.
4. Response widgets and feed.
5. Tool join/virtual content/full-screen search.
6. Follow behavior/diagnostics/cleanup/docs.

Before considering the implementation finished, perform a separate correctness review focused on:

- concurrent routing and attribution evidence;
- high-water/backfill race handling;
- no UI-thread blocking work;
- virtual rendering complexity;
- preservation of raw SSE fidelity;
- dirty-worktree boundaries and unrelated file preservation.

