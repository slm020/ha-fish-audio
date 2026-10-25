# Fish Audio TTS (streaming) for Home Assistant

> [!WARNING]
> **This is vibe coded and unofficial.** The changes below were written with an AI assistant for one
> person's home setup. They are lightly tested, come **as is** with no support and no guarantees, and may break your
> Home Assistant setup. Read the code and back up before using it. Shared in case it is useful to someone.

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
- **Raw PCM in a WAV wrapper.** Audio is streamed as 16-bit mono PCM (44.1 kHz) behind a WAV header of unknown
  length. HA's ffmpeg probes WAV immediately, but waits for about 4 s of audio before emitting anything from a short
  mp3 stream, which held a one-second opener back until the real answer arrived. The stream is paced to real time
  (a few seconds ahead), and short pieces of near-silence are added if the speaker is about to run out of audio
  while the answer is still being written. Note that some firmware also holds the end of a short clip until more
  audio follows (see ESPHome micro-decoder before 0.3, which blocks on a full 8 KB read); digital silence compresses
  to almost nothing in FLAC so it cannot flush that, which needs a firmware fix.
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

This is based on the Home Assistant core `fish_audio` integration by @noambav, licensed Apache-2.0 (see `LICENSE`).
The files were modified as described above. The core integration is the upstream; the changes here could be offered
to it as a pull request.
