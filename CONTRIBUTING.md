# Contributing

Contributions should keep the project small, local-first, and limited to
authorized single-video downloads and local creator-library transcription.

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

## Contribution grant

By submitting a contribution, you represent that you have the right to submit
it and grant the repository owner a perpetual, worldwide, non-exclusive,
royalty-free right to use, reproduce, modify, distribute, sublicense, and
commercially license the contributed material as part of this project.

This grant covers only material you have the right to contribute. Third-party
material remains under its original license.
