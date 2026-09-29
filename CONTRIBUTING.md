# Contributing

Contributions should keep the project small, local-first, and limited to
authorized single-video downloads, local creator-library transcription, and
rewriting from a validated existing library. Keep the root Skill usable without
a companion Skill or a new hosted service.

## Before submitting

1. Do not include real videos, cookies, tokens, account data, private links, or
   copied platform responses.
2. Add or update synthetic tests for behavior changes.
3. Run `python3 -m unittest discover -s tests -v`.
4. Run `python3 -m py_compile scripts/*.py tests/*.py` and check shell syntax.
5. Document any third-party code, algorithm, or text that was copied or adapted.

Use synthetic fixtures for catalog, download, and ASR behavior. Browser login
profiles, model weights, transcripts, and real media belong outside the source
repository. Build archives with `scripts/package_skill.py`, which uses a release
allowlist; do not zip a live installed Skill directory recursively.

For batch rewriting, use synthetic catalog entries, ASR segments and draft JSONL.
Verify unique source-ID coverage, stable likes ordering, missing-metric handling,
opening preservation, source-drift detection and protection of edited output.
Do not add real author biographies, source scripts or account-specific counts to
generic examples. Keep working JSONL and state in WORK and final artifacts in OUT.

The deterministic helpers must not call online LLMs or model Computer Use.
Writing and semantic review are separate model work. A successful structural
check does not certify semantic accuracy, factual accuracy or audio review;
repetition and language flags are prompts for contextual review. Public metrics
are dated observations, and the reference score is an editorial aid, not a claim
about plays or the platform algorithm. Missing counts must not become zero.

Keep shared routing and essential constraints in `SKILL.md`; place the batch
schema and procedures in `references/batch-rewrite.md`. Preserve single-video
behavior when changing the batch route.

## Contribution grant

By submitting a contribution, you represent that you have the right to submit
it and grant the repository owner a perpetual, worldwide, non-exclusive,
royalty-free right to use, reproduce, modify, distribute, sublicense, and
commercially license the contributed material as part of this project.

This grant covers only material you have the right to contribute. Third-party
material remains under its original license.
