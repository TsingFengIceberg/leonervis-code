# Derived Design Notes for Leonervis Code

These are independent design conclusions from studying the pinned Claw-Code prompt surfaces. They are not Claw-Code prompt text and do not imply feature parity.

## 1. Treat prompt content as several contracts

A coding harness does not have only one prompt string. Its model-visible contract includes:

- stable system behavior and Host/model responsibility;
- dynamically rendered environment and workspace context;
- tool names, descriptions, schemas, and permission semantics;
- model-visible errors, refusals, reminders, and stop conditions;
- compaction continuation and synthetic summaries;
- optional project instructions or output styles.

Leonervis should maintain each concern in one canonical source and assemble them deterministically. Putting every rule into a monolithic system prompt would duplicate tool and policy truth; scattering the same rule into adapters and UI would create drift.

## 2. Keep stable and dynamic content separate

A stable prompt prefix is easier to review, fingerprint, test, and cache. Per-session or per-turn values—workspace, date, runtime snapshot, permission state, budget—belong in later structured sections. Dynamic values should not be interpolated into the stable core merely because one API accepts a single string.

For Leonervis, this suggests a future prompt builder with explicit typed inputs rather than ad hoc f-strings spread across providers. Provider adapters should serialize the already-built prompt; they should not own product policy prose.

## 3. Version the effective contract, not a hand-edited label

A useful prompt identity should be derived from canonical, normalized content and all other model-visible prompt surfaces that the version promises to cover. A manually bumped number without a reproducible fingerprint cannot prove what a historical turn saw.

The initial slice can remain small: one versioned stable system prompt, deterministic assembly, and tests that assert its meaningful sections. More elaborate manifests are unnecessary until additional prompt surfaces exist.

## 4. Tools remain authoritative about tools

The system prompt can explain general tool-use behavior, but each tool's current name, purpose, trigger conditions, arguments, and constraints belong in its schema/description. Adding write, edit, search, Bash, or permission-aware tools therefore requires reviewing both:

1. the general system contract; and
2. the tool's own model-visible definition.

A prompt must never advertise a tool that the request does not actually expose.

## 5. Prompt instructions do not enforce hard boundaries

Statements about workspace containment, permission, output limits, timeouts, or edit conflicts help the model choose good actions; they do not provide security. Leonervis must continue enforcing these rules in the Host even when the system prompt describes them. Conversely, a Host capability should not remain invisible to the model if using it correctly depends on model awareness.

## 6. Compaction is its own prompt surface

A compacted conversation changes what the model sees and may introduce synthetic system content. Its continuation text, summary schema, tool-pair preservation, audit record, and failure fallback need independent design and tests. Claw-Code's deterministic summary path is one reference approach; Leonervis should decide its own summary inputs and trust boundaries before adding `/compact`.

## 7. Separate model prompts from human prompts

A terminal approval question is a Host-to-human interface. Permission reasons returned after denial may also become model-visible tool results. These are related but distinct contracts and should not share a string merely for convenience. The UI can evolve without changing model policy, while model-visible denial results must remain deterministic and auditable.

## 8. Project instruction files are untrusted context

If Leonervis later reads `LEONERVIS.md`, `CLAUDE.md`, or other rules, the Host must define discovery order, scope, size limits, deduplication, provenance, and instruction authority. File content should not be confused with the immutable product system prompt, and tool output must never acquire system authority just by containing instruction-like tags.

## 9. Reference material should point, not fork

The pinned submodule already gives offline access to exact upstream sources. A local map adds navigation and Leonervis-specific analysis; a copied prompt corpus would add stale duplicate truth and attribution work. Therefore the reference layer should remain commit-pinned links plus paraphrased notes unless a narrowly justified excerpt is approved.
