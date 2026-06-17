# DRAFT — DO NOT POST YET

Future GitHub discussion/issue for roflcoopter/viseron. Held for publication
at a later date. Source: fork commit 0c596701 (publish snapshot URLs instead
of raw image bytes over MQTT) — to be upstreamed only as an opt-in flag.

---

**Title:** Proposal: opt-in MQTT snapshot delivery via URL (image platform) alongside existing raw-bytes camera entity

**Body:**

### Motivation
Viseron currently publishes camera snapshots over MQTT as raw JPEG bytes (HA `camera` platform). On busy setups with many cameras this puts non-trivial, continuous bandwidth on the broker (full JPEG blobs, retained). I'd like to offer a lighter alternative: publish a small JSON payload containing a snapshot **URL** and let Home Assistant fetch the image on demand via the `image` platform.

I have this working on my fork and would like to upstream it — but **only behind a config flag**, because done unconditionally it's a breaking change (see below). Opening this for maintainer direction before I send a PR.

### The compatibility problem
Switching delivery isn't just a payload change — it changes the **HA entity platform**:
- Discovery flips from `camera` → `image` (`url_topic` + `url_template` instead of raw `topic`).
- On upgrade, every existing `camera.viseron_*` entity goes unavailable and a new `image.viseron_*` appears, breaking dashboards/automations that reference the old entity.

So existing users must be able to keep today's behavior untouched.

### Proposed design
New component-level option, **defaulting to current behavior**:

```yaml
mqtt:
  image_publish_mode: bytes   # "bytes" (default, current camera platform) | "url" (image platform)
```

- `bytes` — exactly today's behavior; no change for anyone who doesn't opt in.
- `url` — publishes `{ "state": "<snapshot_url>", "attributes": {…} }` and emits `image`-platform discovery.

### Open questions for maintainers
1. **Config granularity** — component-level (simplest) vs per-camera override?
2. **Access token in URL** — my prototype embeds `?access_token=…` in the URL, which then sits in a *retained* MQTT message and goes stale if the token rotates. Is there a preferred auth approach for HA fetching the snapshot (e.g. a dedicated/long-lived token, or relying on HA's own auth to the Viseron URL)?
3. **Naming** — `image_publish_mode` ok, or do you prefer a boolean / different key?

Happy to implement once there's agreement on shape + the token question. Implementation keeps both code paths and makes the HA discovery domain config-derived.
