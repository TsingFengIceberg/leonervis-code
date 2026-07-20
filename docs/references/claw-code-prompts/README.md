# Claw-Code Prompt Reference

This directory is a documentation-only study index for prompt-related behavior in the pinned [Claw-Code submodule](../../../learning-submodules/claw-code/). It is not a copy of Claw-Code's prompt corpus and is never imported by Leonervis Code at runtime.

## Reference identity

- Upstream: <https://github.com/ultraworkers/claw-code>
- Local submodule: `learning-submodules/claw-code`
- Inspected gitlink: `4ea31c1bc91c4e9bcbd67d51c550c01e127e6d0d`
- Inspected on: 2026-07-20
- Canonical upstream implementation at this revision: `rust/`
- License: [MIT](../../../learning-submodules/claw-code/LICENSE), copyright © 2026 UltraWorkers and Claw Code contributors

Claw-Code's own README says that it is not affiliated with or maintained by Anthropic, does not claim ownership of original Claude Code source material, and describes the repository as an agent-managed exhibit. Those statements, together with historical references to an unavailable archived TypeScript snapshot, make verbatim prompt copying an unnecessarily unclear provenance path even though the current repository carries an MIT license.

## What this directory contains

- [Prompt source map](./prompt-map.md): a commit-pinned, paraphrased inventory of active prompt surfaces and historical metadata.
- [Maintenance and provenance policy](./maintenance-policy.md): how to refresh the inventory and when an excerpt would require attribution.
- [Derived design notes](./derived-design-notes.md): architectural lessons considered independently for Leonervis Code.

## Deliberate boundary

Leonervis Code uses this material for behavioral study only:

- no source file under `src/` imports the submodule or these documents;
- no entire system prompt, tool-description catalog, transcript, or archived prompt is reproduced here;
- source references use stable symbols and paths rather than line-number snapshots;
- Leonervis Code's future system prompt remains an independently written, capability-accurate runtime contract;
- a Claw-Code capability is not a Leonervis requirement until a Leonervis slice has its own problem statement, boundaries, implementation, and tests.

When reading a source, use the pinned submodule directly. If the gitlink moves, treat this inventory as stale until it is reviewed according to the maintenance policy.
