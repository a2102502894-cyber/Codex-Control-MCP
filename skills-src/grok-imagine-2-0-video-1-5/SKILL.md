---
name: grok-imagine-2-0-video-1-5
description: Use for Grok Imagine 2.0 image generation, Grok image edit, and Grok Imagine Video 1.5 through grok2api.
version: 1.0.1
---

# Grok Imagine 2.0 + Video 1.5

Use this skill when calling the local or public grok2api gateway for Grok image generation, image editing, or video generation.

## Endpoint preference

- On the same host as grok2api, prefer its loopback origin such as `http://127.0.0.1:8000/v1` when that caller actually runs on the grok2api machine. Do not confuse a remote caller's localhost with the grok2api host's localhost.
- For remote/cloud callers that cannot reach localhost, use the configured public gateway, currently `https://grok2api-box.aiwsb.site/v1`.
- Authentication is `Authorization: Bearer <Build+Web or Web/Console-capable key>`.
- Never expose credentials in tool arguments, logs, prompts, or user-visible output.
- Health probe: `GET /healthz`. Authenticated `GET /v1/models` is valid. Public 530/1033 means tunnel failure, not a media payload error.

## Model and endpoint map

| Intent | Model | Endpoint | Completion |
| --- | --- | --- | --- |
| Text to image | `grok-imagine-image-2.0` | `POST /v1/images/generations` | synchronous |
| Image edit | `grok-imagine-image-edit` | `POST /v1/images/edits` | synchronous, often ~60s |
| Text/reference to video | `grok-imagine-video-1.5` | `POST /v1/videos/generations`, then `GET /v1/videos/{request_id}` | asynchronous; terminal success is `status=done` |

Do not use `grok-imagine-image-2.0` on `/images/edits`. Use `grok-imagine-image-edit` for edits.

## Image generation

Preferred JSON body:

```json
{
  "model": "grok-imagine-image-2.0",
  "prompt": "...",
  "n": 1,
  "aspect_ratio": "1:1",
  "response_format": "b64_json"
}
```

Supported Web-route aspect ratios are currently:

`auto`, `1:1`, `16:9`, `9:16`, `4:3`, `3:4`, `3:2`, `2:3`, `2:1`, `1:2`, `19.5:9`, `9:19.5`, `20:9`, `9:20`.

`4:5` is not supported on this deployment. Do not assume arbitrary ratios work.

OpenAI-style size aliases known to map correctly include `1024x1024`, `1024x1536`, `1536x1024`, `1280x720`, `720x1280`, `1792x1024`, and `1024x1792`, but prefer `aspect_ratio` on Web routes.

Prefer `response_format=b64_json` for deterministic local saving. Returned bytes may be JPEG even when a caller expected PNG. Trust `mime_type` and file magic, not the requested extension.

## Image edit

Preferred JSON body:

```json
{
  "model": "grok-imagine-image-edit",
  "prompt": "...",
  "image": {"url": "data:image/png;base64,..."},
  "response_format": "b64_json"
}
```

Rules:

- `image.url` may be an HTTP(S) URL or a `data:image/...;base64,...` URL.
- Prefer JSON, not multipart, on this deployment.
- Use `response_format=b64_json` for final edit output. Do not depend on a URL response when final-vs-partial image selection is uncertain.
- Web image edit currently supports `resolution=1k` only. Do not request `2k`.
- If resolution is not required, omit it.
- Allow at least 180 to 240 seconds of client timeout.
- Avoid hard parallel bursts. The Web edit path may serialize through a single browser worker.
- A tiny or visibly blurred 171x256-style image is a failed/partial preview, not an acceptable final asset. Reject it in QA and do not promote it.
- The browser worker/signer must not select the first visible `<img>` blindly. Prefer explicit final evidence (`isFinal`, `progress=100`, or equivalent) when available; otherwise rank candidates by rendered/natural pixel area and reject obviously small preview assets. Trusted-CDN, `data:` and `blob:` sources can all be previews, so source scheme alone is not proof of finality.
- Client wrappers should fail closed on an obviously undersized edit result. On this deployment, a practical guard is a minimum image edge of 512 px unless a task explicitly documents a smaller valid target. This guard prevents promotion of previews; it does not replace final-image selection in the worker.

## Video generation

Create:

```json
{
  "model": "grok-imagine-video-1.5",
  "prompt": "...",
  "duration": 5,
  "aspect_ratio": "16:9",
  "resolution": "720p"
}
```

The create call returns `request_id`. Poll `GET /v1/videos/{request_id}` about every 5 to 10 seconds.

Treat `status=done` as terminal success. Do not wait for `completed`.

On success, read `video.url` and save the media. Do not automatically replay expensive video/edit requests after non-retryable failures unless an explicit retry policy allows it.

## Grok-MCP integration contract

When implementing or reviewing Grok-MCP:

1. `grok_image_generate` defaults to `grok-imagine-image-2.0` and should prefer final `b64_json` decoding.
2. `grok_image_edit` must always use `grok-imagine-image-edit`; default final response must be `b64_json`.
3. Edit schema must not advertise unsupported `2k` resolution.
4. Aspect ratio schema should enumerate the deployment-supported ratios rather than accept arbitrary strings.
5. Same-host execution may prefer `http://127.0.0.1:8000/v1`; remote callers must use their configured route to the grok2api host. HTTPS remains required for non-loopback gateways.
6. `grok_video_generate` waits for `done`, not `completed`.
7. Save image files using MIME/magic-byte-derived extensions.
8. Preserve server-side credentials. Tool callers never provide keys.
9. Reject clearly undersized edit outputs before reporting success; include decoded dimensions in diagnostics when possible.

## Production image workflow

For character continuity or other reference-sensitive production work:

1. Generate and visually approve a base identity image.
2. Use `grok-imagine-image-edit` from the approved base image for alternate angles, clothing states, injuries, or other continuity variants when possible.
3. Inspect every generated result visually and technically before promotion.
4. Reject malformed anatomy, wrong identity, costume discontinuity, partial previews, wrong aspect, or unintended text/watermarks.
5. Iterate with edit from the last approved identity source rather than independently regenerating a new face.

## Quick failure checklist

1. `GET /healthz` returns 200.
2. Authenticated `/v1/models` lists the required model IDs.
3. Model matches endpoint, especially edit model vs generation model.
4. Public 530/1033 means tunnel issue.
5. Edit timeout is at least 180 seconds.
6. Edit output is full-resolution/final, not a partial preview.
7. Video polling checks `status=done`.
