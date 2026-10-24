# Fish Audio TTS (streaming) for Home Assistant

A custom version of Home Assistant's built-in [Fish Audio](https://www.home-assistant.io/integrations/fish_audio)
integration, which has the same `fish_audio` domain and overrides the core one when installed in
`custom_components`.

## Changes from the core integration

- **WebSocket streaming.** Each reply goes over one Fish Audio WebSocket. Text from the conversation agent is
  split into sentences as it arrives, each sentence is flushed to Fish straight away, and the audio is passed on
  as it is generated, so speech starts after the first sentence instead of after the whole reply. (Fish's HTTP
  streaming endpoint only returns the body once the full text is synthesised, so it cannot do this.) If the
  socket fails before any audio, it falls back to the older per-sentence HTTP method.
- **Instant short openers.** A reply's first sentence of 30 characters or fewer ("Sure.", "On it.") is stored
  once in `/config/fish_ack_cache` and replayed from disk, so it starts with no Fish delay. The standard
  phrases are generated at start-up; any other short opener is cached the first time it is used.
- **Paid-model fallback.** If a `*-free` model errors or is retired, the integration uses `s2.1-pro` for five
  minutes instead of going silent (this costs money, about $15 per million UTF-8 bytes).
- **More options.** Speed, output format (mp3/opus/wav), MP3/Opus bitrate and chunk length, as well as
  backend model and latency mode.
- **Extra requirement.** `sentence-stream`.

## Measured latency (HA to first audio, one ~140 character reply)

| model | HTTP (body arrives when done) | WebSocket (first audio) |
|---|---|---|
| s2.1-pro-free | ~4.0 s | ~1.2 s (generates slower than real time, can stutter) |
| s2.1-pro | ~2.1 s | ~0.8 s |
| s2-pro | ~1.4 s | ~0.8 s |

`balanced` latency is the one to use (`low` is rejected by fish-audio-sdk 1.1.0).

## Install

Copy `custom_components/fish_audio` into your HA config's `custom_components` folder and restart Home Assistant.

## Credits

This is based on the Home Assistant core `fish_audio` integration (Apache-2.0) by @noambav.
