# Maintenance and Provenance Policy

## Refreshing this reference

The prompt map is tied to the parent repository's Claw-Code gitlink. Whenever that gitlink changes:

1. compare the new gitlink with the inspected commit recorded in [README.md](./README.md);
2. confirm that the submodule checkout matches the gitlink and is clean;
3. re-inspect active Rust prompt assembly, built-in and dynamic tool definitions, compaction, permission text, and any secondary harness prompts;
4. distinguish current runtime source from companion Python metadata, archived path hints, tests, and transcripts;
5. update changed responsibilities and stable symbol anchors;
6. review upstream `LICENSE`, README ownership/non-affiliation statements, and relevant provenance notes;
7. record the new commit and inspection date only after the review is complete.

Until these steps are complete, a SHA mismatch means the docs are an older study snapshot, not a description of the new submodule revision.

A future drift check may compare the documented SHA against the gitlink emitted by:

```text
git ls-tree HEAD learning-submodules/claw-code
```

That check should report drift only. It must not automatically rewrite the map or copy upstream content.

## Copying boundary

The default is **no verbatim prompt copies**. Prefer:

- links to the pinned local source;
- paraphrased responsibility maps;
- independently written design notes;
- Leonervis-specific ADRs explaining adoption or divergence.

Reasons include staleness, unnecessary duplication, and unclear provenance around historical source material described by Claw-Code itself.

If a minimal excerpt becomes necessary for analysis:

1. obtain repository-owner review before adding it;
2. keep it no longer than needed to demonstrate the specific point;
3. place it under `docs/references/claw-code-prompts/excerpts/<commit>/`;
4. identify upstream URL, exact commit, source path, copyright holder, license, and whether the excerpt was modified;
5. preserve the required MIT notice in an appropriate repository-level third-party notice or license file;
6. never make the excerpt a runtime import, template, generated source, or canonical Leonervis prompt.

No excerpt directory or third-party notice is added now because these documents contain no copied prompt passage.

## Independent implementation rule

When Leonervis adopts a learned principle, its implementation must be justified by Leonervis's current capability contract and tests. Similar concepts—such as stable/dynamic prompt separation, deterministic compaction, or permission-aware tool descriptions—do not justify copying wording, data structures, or a complete subsystem.

This policy is an engineering provenance practice, not legal advice.
