# Third-party notices

## Bundled dependencies

This release bundles no third-party Python package, model weights, browser
binary, or source file from the projects listed below. It does not bundle the
F2/DTK request signers or browser credential readers used in earlier experiments.
The downloader uses Python's standard library and the user-installed `curl`;
`ffprobe` is optional for single-video downloads and required for batch validation.

## Optional installed runtimes

Transcription installs MLX Whisper and its dependencies into a private local
virtual environment. Browser collection optionally installs Playwright's Python
client and controls an existing official Microsoft Edge installation; it does
not download a replacement browser. These packages retain their own upstream
licenses and are not included in the Skill release archive.

- MLX: https://github.com/ml-explore/mlx — MIT
- MLX Whisper: https://github.com/ml-explore/mlx-examples/tree/main/whisper — MIT
- Playwright Python: https://github.com/microsoft/playwright-python — Apache-2.0
- FFmpeg: https://ffmpeg.org/legal.html — terms depend on the user's build

Model files are a separate download. Their model card and license govern their
use; the repository's source license does not grant rights to model weights.

## Public references

- `Evil0ctal/Douyin_TikTok_Download_API`
  - URL: https://github.com/Evil0ctal/Douyin_TikTok_Download_API
  - Upstream license: Apache-2.0
  - Relationship: conceptual and product-behavior reference only
- `ucmao/media-parser`
  - URL: https://github.com/ucmao/media-parser
  - Upstream license: MIT
  - Relationship: conceptual and architecture reference only
- `yangbuyiya/yby6-video-parser-skill`
  - URL: https://github.com/yangbuyiya/yby6-video-parser-skill
  - Upstream license: MIT
  - Relationship: Agent Skill packaging reference only
- `yzfly/douyin-mcp-server`
  - URL: https://github.com/yzfly/douyin-mcp-server
  - Upstream license: Apache-2.0
  - Relationship: product-form and maintenance reference only

References and attribution do not imply endorsement by the upstream authors.
Douyin and related marks belong to their respective owners.
