# Blaze AI for macOS

Blaze AI is a local-first desktop assistant for macOS. It combines an offline
command butler with optional on-device chat, computer-control, and voice models.

> **Windows version coming soon.** This release is for macOS only.

## Download

Open **Releases** and download one of these macOS packages:

- `BlazeAI.pkg` — installer that places Blaze AI in `/Applications`.
- `BlazeAI-<version>.dmg` — drag **Blaze AI.app** to Applications.
- `BlazeAI-macOS.zip` — app bundle and setup README.

Each release includes a macOS README with install steps and known limits. This
community build is not Developer ID signed or notarized, so Gatekeeper may ask
you to confirm that you trust the release source.

## What it can do

- Open and close installed apps, search the web, open supported media searches,
  download direct HTTP/HTTPS file URLs, capture screenshots, record the screen,
  and report system resources.
- Use local speech recognition for push-to-talk commands. Spoken replies use
  the operating system's available voice.
- Load a compatible local chat model for general conversation and multi-step
  computer-control tasks.
- Use local agent tools for supported app automation, files, typing, keyboard
  and mouse input, and media editing.

While Blaze is speaking, press and hold the mic button or press
**Ctrl+Shift+Space** to interrupt and start talking. Use the stop button or
**Esc** to stop a generated or spoken reply. Blaze does not listen for a wake
word or automatically detect speech while it is speaking.

Chat replies use a shorter recent context and a 96-token response limit to
reduce local generation time. All chat history remains saved; older messages
are omitted from the prompt sent to the local model. Smaller quantized models
usually respond faster, depending on your hardware.

## Privacy

Chat inference and speech recognition run on the device when local models are
selected. Blaze does not send chat prompts to a hosted AI service. Web search,
direct downloads, and optional model downloads require internet access and
connect to the services you request. Chats, settings, and contacts are stored
in your macOS user profile.

Model weights are not included in app downloads. Choose a compatible `.gguf`
model file or Transformers model folder in Settings. General chat and
multi-step automation require a loaded chat model; deterministic butler
commands work without one. Voice input needs the speech dependencies and a
local speech model.

## Safety

- Agent actions ask for approval by default. Shell commands, file writes,
  keyboard and mouse input, clipboard writes, AppleScript, and ffmpeg always
  require a separate approval.
- Checks block selected destructive commands and model-driven SSH, SSH server,
  and common tunneling or port-forwarding commands.
- Downloads validate public network addresses and redirects, enforce size and
  duration limits, and avoid overwriting existing files. Downloaded files are
  not opened or run automatically.
- Message and model response sizes are bounded. SQLite values use parameterized
  queries, and displayed messages are escaped and sanitized.

These safeguards reduce risk but cannot guarantee protection from every
current or future vulnerability. Review each action before approving it.

## Requirements and limits

- macOS 11 or later; Apple Silicon is recommended for local model inference.
- Searching and downloading need an internet connection. Messaging and app
  automation depend on installed apps and macOS privacy permissions.
- Blaze cannot perform every task available to Siri or Google Assistant. Some
  apps expose no automation interface, and some actions need user input.

## Source and building

This repository includes the Blaze AI source code and tests. Build native apps
on macOS using the included build scripts. GitHub Actions can build and attach
the macOS downloads when a version tag such as `v1.1.0` is pushed.

## Support

Open an issue with your macOS version, steps to reproduce the problem, and any
relevant error message. Do not include passwords, private keys, personal
messages, or other sensitive information.
