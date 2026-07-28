# Comprehensive Code Review Remediation Design

Date: 2026-07-28

## 1. Purpose

Repair every confirmed issue from the current code review while preventing the
review or its implementation agents from adding unrelated features, MCP
servers, plugins, tools, dependencies, or configuration.

“Comprehensive” means that every confirmed issue receives an explicit
implementation task or an explicit evidence-based exclusion. It does not grant
permission for opportunistic refactoring.

## 2. Program Structure

The work is divided into seven independently reviewable packages:

1. Model-context lookup responsibility migration.
2. Local Embedding and vector-memory correctness.
3. Backend duplicate removal.
4. Frontend lint and dead-code cleanup.
5. Frontend settings-save and model-preset consolidation.
6. Frontend bundle and chunk optimization.
7. Cross-package verification and documentation.

Packages that modify overlapping files run serially. Packages may only run in
parallel when their file allowlists are disjoint.

Each package must have its own branch or worktree, tests, review, and commits.
No implementation agent may write directly to `main`.

## 3. Non-Negotiable Execution Guardrails

Every implementation task must state all of the following:

- Do not install, connect, configure, remove, or modify any MCP server.
- Do not install or modify Codex plugins, skills, or external development
  tools.
- Do not change MiniUnicorn's MCP catalog, MCP presets, MCP registration,
  MCP lifecycle, or configured MCP servers.
- Preserve automatic online model-context discovery in model configuration and
  onboarding flows. Remove it from Agent construction, provider snapshot
  creation, preset creation, and runtime model switching.
- Preserve chat-provider selection, fallback, retry, and runtime-switching
  behavior unless a task explicitly names one line needed to detach memory
  Embedding from chat.
- Do not touch pre-existing untracked files.
- Do not run broad formatters or mechanical rewrites across the repository.
- Do not modify files outside the task's allowlist.
- Stop immediately if a required change crosses a forbidden boundary.
- Do not stage generated output, caches, decoded assets, local configuration,
  or unrelated changes.

Before starting a package, the coordinator records:

```powershell
git rev-parse HEAD
git status --short
git diff --name-only
```

After the package, the coordinator compares the changed paths with the
allowlist. Any unexpected path rejects the package before code review.

The only approved new runtime dependency is the CPU `fastembed` dependency in
the existing `vector` optional extra. Frontend packages may not change
`package.json` or any lockfile.

## 4. Package A: Model-Context Resolution Boundary

### 4.1 Responsibility Boundary

Hugging Face and ModelScope lookup is configuration-time discovery, not an
Agent runtime responsibility.

The accepted flow is:

```text
add or edit model
        │
        ├─ explicit contextWindowTokens → validate → persist
        │
        └─ missing contextWindowTokens
                │
                ├─ learned cache hit → persist concrete integer
                │
                └─ cache miss → query Hugging Face, then ModelScope
                                  │
                                  ├─ success → persist concrete integer
                                  └─ failure → apply incomplete-model rules

Agent start / preset build / provider switch
        │
        └─ read the already persisted integer or reject the unusable model
```

The online lookup implementation may remain in `miniunicorn/cli/models.py` so
configuration and onboarding code can reuse it. Agent core and provider-runtime
modules must not import it, call it, read its cache, or initiate equivalent
network traffic.

### 4.2 Runtime Contract

Runtime consumers receive a resolved positive integer. A small pure validation
boundary, such as `require_context_window(model, configured_value)`, returns the
integer or raises a dedicated `UnresolvedModelContextError`.

That validator:

- performs no network access;
- performs no cache lookup;
- has no provider-specific fallback;
- does not guess a default such as `65536`;
- produces an error that identifies the unusable model and tells the operator
  to complete its configuration.

The following current runtime lookup paths are removed:

- Agent construction in `miniunicorn/agent/loop.py`;
- primary and fallback snapshot construction in
  `miniunicorn/providers/factory.py`;
- static preset construction in `miniunicorn/agent/model_presets.py`;
- provider snapshot application in
  `miniunicorn/agent/_provider_switching.py`.

Startup and switching continue to calculate limits and preserve all existing
provider behavior, but only from validated stored values.

### 4.3 Save-Time Resolution

Model-save handlers resolve a missing context before committing the
configuration:

1. validate a manually supplied value and skip network discovery when valid;
2. check the existing learned-context cache;
3. query Hugging Face;
4. if needed, query ModelScope;
5. validate the discovered value;
6. persist it as the model's concrete `contextWindowTokens`;
7. update the learned cache only as configuration-stage metadata;
8. commit the configuration atomically.

The learned cache remains an optimization and a source indicator for the
settings display. It is not a second runtime configuration source. Existing UI
code may infer “learned” versus “manual” by comparing the cached model and
value; no new runtime resolution-state field is introduced.

CLI onboarding follows the same rule: successful discovery must be written into
the generated model configuration. A hand-edited configuration that omits the
value fails clearly at startup or switching; runtime does not repair it.

### 4.4 Failure Semantics

Failure must never silently create a runnable model with a guessed limit:

- a newly added model whose lookup fails may be saved as incomplete, but it is
  inactive and cannot become the default;
- editing an inactive model may save it as incomplete and keep it inactive;
- when editing the active or default model, lookup failure leaves the previous
  usable persisted configuration unchanged, while the frontend retains the
  user's unsaved draft and displays the error;
- switching to an incomplete model is rejected and the currently active model
  remains unchanged;
- an unresolved fallback model rejects the provider configuration or startup,
  because the safe combined context limit cannot be calculated;
- a valid manual context value bypasses discovery and is saved normally.

These rules do not change provider selection, retry, fallback, or runtime
switching semantics for fully configured models.

### 4.5 Request and Concurrency Model

Existing synchronous discovery runs in a worker thread through
`asyncio.to_thread()` with one explicit overall timeout. The model-save HTTP and
WebSocket handlers become asynchronous where necessary and wait for the single
save result.

The current “start a background thread, return early, then poll for learned
state” save behavior is removed. While awaiting the response, the frontend
shows that context is being queried and keeps the draft intact.

Discovery must not hold the configuration write lock:

1. read the target provider and model identity;
2. release the configuration write path;
3. perform discovery;
4. reload the latest configuration;
5. verify that the provider/model target still matches the request;
6. discard a stale result if the target changed;
7. otherwise perform one atomic configuration save.

This prevents a slow lookup from overwriting a newer edit.

### 4.6 Allowed Scope

Likely implementation paths for this package are limited to:

- `miniunicorn/cli/models.py`;
- `miniunicorn/webui/model_settings_api.py`;
- `miniunicorn/channels/websocket/handlers/settings.py`;
- `miniunicorn/providers/factory.py`;
- `miniunicorn/agent/loop.py`;
- `miniunicorn/agent/model_presets.py`;
- `miniunicorn/agent/_provider_switching.py`;
- `miniunicorn/config/schema.py`;
- `webui/src/components/settings/hooks/useModelsAndProvidersSection.ts`;
- focused backend and frontend tests for the named behavior.

The implementation agent must narrow this list further per task. This package
does not authorize changes to MCP, Embedding, chat retry, provider fallback
policy, unrelated settings, or dependencies.

### 4.7 Acceptance Criteria

- Agent startup, provider snapshot construction, preset creation, and model
  switching produce zero Hugging Face or ModelScope requests.
- Core tests replace the lookup function with a failure sentinel and still pass,
  proving runtime code cannot call it.
- Successful automatic discovery persists the concrete integer in the model
  configuration.
- A valid manual value causes zero discovery requests.
- A failed new-model lookup produces an inactive incomplete model.
- A failed active-model edit preserves the prior usable stored configuration
  and the frontend draft.
- Switching to an incomplete primary model is rejected without changing the
  active model.
- An unresolved fallback model is rejected.
- A stale concurrent discovery result cannot overwrite a newer edit.
- Configuration-time Hugging Face and ModelScope fallback behavior remains
  functional under mocked HTTP tests.
- No MCP, Embedding, chat retry, or unrelated provider behavior changes.

## 5. Package B: Local Embedding

### 5.1 Product Behavior

Vector memory uses a local CPU model independently of the chat provider:

```text
conversation/history text
        │
        ▼
LocalEmbeddingProvider
BAAI/bge-small-zh-v1.5, CPU, 512 dimensions
        │
        ▼
memory/memory.db
        │
        ▼
semantic recall → Agent context
```

Chat may continue to use DeepSeek, Claude, OpenAI-compatible endpoints, or
fallback providers. Switching chat providers must not replace or reconfigure
the local Embedding object.

The default local model is `BAAI/bge-small-zh-v1.5`. FastEmbed's maintained
model list identifies it as a Chinese 512-dimensional model with an
approximately 0.09 GB model download:

- https://qdrant.github.io/fastembed/examples/Supported_Models/
- https://github.com/qdrant/fastembed

CPU is mandatory for this package. GPU and CUDA dependencies are not added.

### 5.2 Dependency Boundary

The existing `vector` optional dependency group contains both:

- `sqlite-vec`
- `fastembed>=0.8.0,<0.9.0`

The core installation remains free of the local model runtime. Enabling vector
recall without the `vector` extra must not break chat.

No `sentence-transformers`, `torch`, GPU package, vector database, Zvec, model
server, or new service is added.

### 5.3 Configuration

Keep `agents.defaults.vectorRecall`, defaulting to `false`.

Keep `agents.defaults.embeddingModel`, but change its default and documented
meaning to the local model ID `BAAI/bge-small-zh-v1.5`.

Remove the unused external-service configuration:

- `providers.embeddingProvider`
- `providers.embeddingModel`
- `providers.embeddingApiBase`
- `providers.embeddingApiKey`

Remove the unused external endpoint wrapper
`miniunicorn/providers/embedding.py`.

Do not remove `LLMProvider.embed()` or `OpenAICompatProvider.embed()`. They may
be part of the Python provider API, but memory retrieval will no longer call
them.

No new WebUI settings section is introduced. This repair connects and
corrects existing configuration; it does not add a new product surface.

### 5.4 Local Provider

Add one focused local provider with this contract:

```python
class LocalEmbeddingProvider:
    model_name: str
    dimension: int

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]: ...
```

Required behavior:

- model loading is lazy;
- the execution provider is CPU;
- blocking model load and inference run outside the event loop;
- concurrent first calls load the model once;
- empty input returns an empty list without loading the model;
- output is normalized and converted to `list[list[float]]`;
- every returned vector is exactly 512 elements;
- a model override different from the configured local model is rejected;
- missing dependency, missing cache while offline, download failure, and
  inference failure produce a clear diagnostic and return control to the
  existing non-vector memory path.

Tests mock FastEmbed. A separate optional smoke test performs one real model
download and verifies a 512-dimensional Chinese embedding.

### 5.5 Vector Store

New vector-memory databases use dimension 512. The database records a small
metadata fingerprint containing:

- schema version;
- model ID;
- vector dimension.

The project has not had a production release, so no automatic 1536-to-512
migration, backup, re-embedding, or compatibility branch is added.

If an existing `memory.db` has no matching fingerprint or has a different
dimension, MiniUnicorn must:

1. leave the file untouched;
2. disable vector recall for that run;
3. log one actionable message telling the developer to remove the development
   database and restart.

It must never silently mix vectors from different models and must never delete
the database automatically.

### 5.6 Runtime Wiring

`AgentLoop` owns two separate dependencies:

- `provider`: chat;
- `_embedding_provider`: local Embedding.

The same `_embedding_provider` instance is passed to `MemoryStore`, automatic
query embedding, and the recall tool. Runtime chat-provider switching must not
modify it.

The full-memory fallback already used by `ContextBuilder` remains the fallback
when local Embedding or `sqlite-vec` is unavailable.

## 6. Package C: Backend Duplicate Removal

Each item is a separate task and commit.

### 6.1 Message Content Merge

Replace the duplicate implementations in:

- `miniunicorn/agent/context.py`
- `miniunicorn/agent/runner.py`

with one shared function. Preserve:

- string plus string joins with two newlines;
- an empty left string returns the right string;
- `None` converts to no blocks;
- non-dict list values convert to text blocks;
- multimodal block order.

Both call sites receive parity tests before extraction.

### 6.2 Progress Callback Signature Detection

Use one helper for the duplicate signature checks in:

- `miniunicorn/agent/progress_hook.py`
- `miniunicorn/utils/progress_events.py`

Preserve failure behavior for uninspectable callables, explicit named
parameters, and `**kwargs`.

### 6.3 Chunked Header Collection

Make `miniunicorn/channels/websocket/_chunked_header.py` the canonical collector.
The limited HTTP version reuses collection but retains its own count and UTF-8
byte checks.

Preserve all current behavior, including:

- base-only headers;
- case-insensitive names;
- numeric ordering;
- non-numeric suffix rejection;
- duplicate index last-value behavior;
- current negative-suffix behavior;
- the exact limit exception type, status mapping, and message.

The refactor must not change MCP header parsing or route tables in
`_http_routes.py`.

### 6.4 HTTP and Query Helpers

Create or reuse a dependency-neutral WebUI HTTP helper for the duplicate
response, error, and case-insensitive header functions currently split between
media and WebSocket modules.

Reuse `miniunicorn/webui/_query.py::_query_first` instead of maintaining the
duplicate in `_http_routes.py`.

Preserve status line, header order, content length, reason text, UTF-8 encoding,
and `Connection: close`.

### 6.5 Dead Backend Code

Delete only the confirmed unused private `_lines_to_text` helper in
`miniunicorn/agent/tools/apply_patch.py`.

Do not remove public or test-imported helpers solely because production `rg`
shows no call. Those symbols are not proven dead and are excluded from this
program.

No task may alter tool decorators, discovery, registration, schemas, names, or
execution behavior.

## 7. Package D: Frontend Lint and Dead Code

### 7.1 Lint Baseline

The current baseline is:

- 10 ESLint errors;
- 7 ESLint warnings;
- 262 passing Vitest tests;
- TypeScript build check passing.

All lint findings are resolved without disabling a rule globally.

### 7.2 Accessibility and Focus

For dialog and form focus:

- preserve the current initial-focus experience;
- replace raw `autoFocus` with controlled focus at open time;
- add interaction tests proving the intended input receives focus.

For clickable non-buttons:

- prefer semantic buttons where layout permits;
- otherwise provide role, tab focus, Enter, and Space behavior;
- preserve existing mouse behavior and styling.

For user-provided audio/video without caption data:

- do not fabricate caption tracks;
- apply a narrow, documented line-level lint exception;
- retain accessible labels and controls;
- do not disable `media-has-caption` globally.

For Hook warnings:

- stabilize callbacks or memoized values where that preserves behavior;
- snapshot mutable refs inside cleanup functions;
- add request-count tests when dependency changes could repeat an API call.

The `McpView` warning may receive a Hook-only fix, but tests must prove the
change does not create, enable, delete, or repeat-load MCP configuration.

### 7.3 Dead Frontend Code

Delete the confirmed unused fixture file and unused private exports identified
by the audit, including:

- `webui/src/lib/app-mentions-fixtures.ts`;
- unused helpers in thread messages, attached images, chat groups, CLI app
  events, MCP mention events, image encoding, storage, tool traces, and view
  registry.

Deleting the fixture file is not permission to modify the runtime MCP catalog
or configuration. The coordinator verifies that no runtime import existed
before deletion.

Do not delete the two pre-existing untracked decoded PNG files. They belong to
the user's working tree and must remain unmodified.

## 8. Package E: Frontend Settings Consolidation

### 8.1 Save Actions

Introduce one small typed save-action primitive. It owns:

- one action's in-flight guard;
- its saving state;
- error normalization;
- payload application;
- restart marking;
- optional host-engine restart;
- cleanup in `finally`.

Each previously independent action receives its own primitive instance so that
the refactor does not unexpectedly make unrelated buttons block one another.

Convert sections incrementally:

1. Web search and web fetch.
2. Image generation.
3. Advanced settings.
4. Runtime settings.
5. Model preset activation and deletion.

For every conversion, tests cover:

- unchanged request payload;
- unchanged request count;
- repeated-click suppression;
- success;
- failure;
- restart-required handling;
- clearing of saving state after failure.

The nested model-configuration saving states are consolidated only after tests
capture the distinct inline and dialog behavior.

### 8.2 Atomic Provider Save

Extend the existing provider-update operation instead of adding another route.
It accepts optional model selection together with credentials and provider
settings.

The backend must:

1. parse and validate all submitted values;
2. update provider credentials and active provider/model on one in-memory
   config object;
3. call the existing atomic `save_config()` once;
4. preserve context-window auto-learning for a changed model;
5. return one settings payload.

The frontend `saveProvider` performs one request. A validation failure writes
nothing. A runtime refresh failure is reported using existing restart semantics
rather than presenting a false “nothing was saved” error.

This task must not touch MCP settings routes or payloads.

### 8.3 Shared Model Preset Select

Add a focused `ModelPresetSelect` component for:

- heartbeat;
- planner;
- image generation.

Its interface explicitly represents:

- the caller's default sentinel;
- selected value;
- disabled state;
- options;
- optional provider icon rendering;
- change callback;
- accessible label.

It does not absorb the existing, more complex `ModelPresetPicker`, which also
owns model-configuration creation behavior.

Tests cover the three different default sentinels (`""`, `null`, and
`"default"`), disabled behavior, provider icon display, selected marker, and
change payload.

## 9. Package F: Bundle and Chunk Optimization

No dependency or analyzer package is added.

### 9.1 Syntax Language Chunks

Preserve `prism-async-light` and its full supported language set.

Replace the current one-file-per-language output with a deterministic small
number of lazy language buckets in Vite `manualChunks`. The bucket algorithm is
stable and based on language-module filename ranges. It must not eagerly load
the buckets on pages that render no highlighted code.

Acceptance:

- the build emits no more than 16 syntax-language chunks;
- a common language and a rare language both highlight correctly;
- plain-code fallback still renders while the lazy chunk loads;
- total JavaScript gzip size does not grow by more than 5%.

### 9.2 Initial Application Chunk

Lazy-load the ready-state chat shell instead of statically importing it into
the authentication/bootstrap shell.

Move the small `resolvedModelProvider` helper out of `ThreadShell` so that a
static helper import does not pull the whole chat shell back into the initial
chunk.

Preserve:

- authentication;
- bootstrap retry;
- loading and error screens;
- the first ready-state chat render;
- model/provider display.

Acceptance:

- no application-owned JavaScript chunk exceeds 500 KB raw;
- total JavaScript gzip size does not grow by more than 5%;
- the number of syntax-language chunks meets the preceding budget;
- all existing navigation and chat tests pass.

Do not raise `chunkSizeWarningLimit` to hide the warning.

### 9.3 Asset Boundary

Tracked Logo and favicon URLs remain unchanged unless a separate visual asset
task proves byte and rendering equivalence.

Pre-existing untracked decoded PNG files are outside this program and are not
deleted, staged, or used as source assets.

## 10. Testing Strategy

Every behavior change follows red-green-refactor:

1. add a focused failing test;
2. run it and confirm the intended failure;
3. implement only the package requirement;
4. run focused tests;
5. run the subsystem suite;
6. review the path allowlist;
7. commit.

Backend verification includes:

```powershell
$env:MINIUNICORN_NO_AUTO_LOOKUP = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"
python -m ruff check miniunicorn tests
python -m ruff format --check miniunicorn tests
python -m pytest -p no:cacheprovider -q
```

The environment flag makes the broad suite deterministic and offline; it does
not change product behavior. Dedicated Agent-start and model-switch tests unset
the flag and replace discovery with a failure sentinel, proving that runtime
does not attempt lookup. Dedicated configuration-stage discovery tests also
unset the flag and mock the HTTP boundary.

Frontend verification includes:

```powershell
cd webui
npm run lint
npm test -- --no-cache
node_modules/.bin/tsc.cmd -p tsconfig.build.json
npm run build
```

The local Embedding unit suite never requires a model download. The explicit
CPU smoke test is separate and may use network access only to download the
approved BGE model.

## 11. Agent Coordination

The coordinator, not the implementation agent, owns integration.

For each agent:

- provide one package or one package task only;
- include exact allowed and forbidden paths;
- provide the base commit;
- require a returned changed-file list and test output;
- reject unrelated cleanup;
- inspect the diff before running integration tests;
- integrate in dependency order.

Recommended order:

1. Model-context resolution boundary.
2. Local Embedding.
3. Backend duplicate removal.
4. Frontend lint and dead code.
5. Frontend save actions.
6. Atomic provider save.
7. Shared preset select.
8. Bundle optimization.
9. Full verification and documentation.

The model-context package runs first because it makes the runtime/configuration
boundary explicit before other provider-facing changes. Backend duplication
touches `context.py`, which also participates in vector recall, so it follows
Local Embedding. Frontend tasks run serially because they share settings hooks
and tests.

## 12. Rollback

Each numbered repair is committed independently. Reverting one repair must not
require reverting unrelated repairs.

Runtime rollback for Embedding is always available by setting
`vectorRecall=false`, which restores the existing non-vector memory path.

If any package:

- changes an unexpected file;
- changes MCP state;
- removes automatic model-context discovery from configuration or onboarding;
- reintroduces model-context network lookup into Agent/runtime code;
- introduces an undeclared dependency;
- fails its focused or subsystem tests;

the coordinator rejects or reverts that package before starting the next one.

## 13. Explicit Exclusions

The following are not confirmed defects and are not changed:

- deletion of automatic online model-context discovery from model
  configuration and onboarding;
- current MCP product behavior and catalog;
- public helpers with no in-repository call but possible external consumers;
- GPU support;
- Zvec or another vector database;
- a document knowledge-base feature;
- a new memory settings UI;
- deletion of user-owned untracked files;
- visual redesign;
- new frontend or development dependencies.
