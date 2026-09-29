# Security policy

## Supported release

Security fixes are applied to the latest published release only.

## Report privately

Do not place private share links, cookies, tokens, proxy credentials, downloaded
videos, or personal information in a public issue. Contact the repository owner
through a private channel listed on the repository and include:

- affected version or commit;
- operating system and Python version;
- a minimal reproduction using a redacted or synthetic link;
- expected and observed behavior;
- whether the issue could expose local files, credentials, or private network
  resources.

## Security boundaries

- The downloader accepts only official Douyin-family share hosts.
- Redirect results and media URLs are validated before use.
- Localhost and literal private-network media addresses are rejected.
- Existing output files are not overwritten.
- Partial files are used while downloading.
- Download and single-video transcription scripts do not read or persist login
  cookies, account credentials, or tokens.
- Creator collection uses an owned, dedicated official Edge profile. Edge may
  retain ordinary login cookies in that private runtime directory. The scripts
  never extract those cookies or read the daily browser's account database.
- The dedicated browser's debugging port must belong to that process and bind
  only to loopback. Its default browser sandbox and TLS validation remain on.
- Only public target-creator metadata is saved from page responses. Request
  headers, signed URLs, and full authenticated responses are not recorded.
- An optional process-local DNS tunnel accepts scoped public HTTPS targets;
  it does not terminate TLS or change system proxy/DNS settings.
- Release archives exclude login profiles, videos, audio, transcripts, model
  weights, and runtime logs. Never publish a working creator-library directory.

This tool cannot guarantee that a remote platform endpoint is always available,
safe, or unchanged. Review changes before running them on sensitive systems.
