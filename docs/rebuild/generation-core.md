# Contract-first generation core

The presentation generation path is split into four narrow boundaries:

1. `ppt_agent/generation/contracts.py` owns versioned JSON contracts and local business validation.
2. `ppt_agent/generation/model_gateway.py` owns structured provider calls, recovery by response ID, replay safety and redacted audit metadata.
3. `ppt_agent/generation/pipeline.py` owns stage orchestration, immutable checkpoints, batch idempotency and atomic offline publication.
4. `ppt_agent/rendering/` owns HTML, CSS, DOM identity, design-token serialization, resource closure and synchronous technical validation.

The workflow service remains the authority for stage changes, user confirmation, versions and jobs. `GenerationPipeline` returns a `StageResult`; it does not update workflow state. Startup wires its preflight into `/readyz`, so model work is admitted only when storage, the locked Chromium executable and the structured generation boundary are ready.

## Contracts

All model-facing responses use strict JSON Schema and the same object is parsed again locally before persistence. A completed response that fails local contract validation may receive one bounded schema-correction call; an uncertain transport result is never replayed. The service owns slide IDs, checkpoint IDs, HTML, DOM attributes, CSS, resource paths and workflow fields. The model produces narrative content, outline content, page blocks, layout-family choices from a finite set and theme-token values.

`TaskBrief`, `NarrativeSpec`, `OutlineSpec`, `SlideSpec` and `DeckSpec` carry `schema_version=1.0`. Content blocks are a finite union: `heading`, `paragraph`, `bullets`, `metric`, `table`, `image` and `quote`.

## Checkpoints and recovery

Every checkpoint contains the task, stage, input version, contract version, model, output SHA-256, parent checkpoint and hashed idempotency key. Checkpoint files and authority records are write-once. A repeated request reads the same checkpoint; conflicting output for the same idempotency key fails closed.

Deck batches use this identity:

```text
task_id + outline_checkpoint_id + sample_checkpoint_id + batch_index + contract_version
```

The sample confirmation checkpoint freezes theme tokens, shared resources, layout families and the exact sample slide specs. The deck aggregator reuses those specs byte-for-byte, orders every page by `OutlineSpec`, validates the complete `DeckSpec`, renders once and commits one authoritative deck checkpoint.

## Renderer and offline delivery

`DeterministicRenderer` emits a fixed 1280×720 canvas, stable slide and element IDs, local resource paths and no executable page content. `TechnicalValidator` rejects unsafe tags, event handlers, remote URLs, resource-closure mismatches, page-order mismatches and text-budget violations. Production startup supplies a fixed Chromium executable for geometry inspection.

Offline publication writes `index.html`, the verified asset closure and `manifest.json` into a temporary sibling directory, verifies all hashes, and atomically renames it into place. The manifest binds contract version, renderer version, deck checkpoint and every file hash.

## Verification

Run the deterministic development gate:

```bash
python -m pytest -q tests/rebuild tests/e2e/test_rebuild_offline_delivery.py
python -m pytest -q tests/browser/test_rebuild_golden_path.py
```

Run real-model gates only from a fixed commit and configuration:

```bash
python scripts/verify_rebuild_release.py --runs 1
python scripts/verify_rebuild_release.py --runs 5
python scripts/verify_rebuild_release.py --runs 20
```

Every repetition receives a fresh task ID and artifact directory. Evidence records commit, tree, configuration hash, stage timing, checkpoint chain, provider call and recovery counts, page order, validation hash, renderer version and offline manifest hash. A failed repetition ends the current gate.
