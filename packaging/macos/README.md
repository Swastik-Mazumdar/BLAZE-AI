# Blaze AI for macOS

Blaze AI is a local-first desktop assistant with an offline command butler and
optional on-device language and voice models. Chat is not sent to a hosted AI
service.

## Install and start

From the DMG download:

1. Open `BlazeAI-<version>.dmg`.
2. Drag **Blaze AI.app** to **Applications**.
3. Open the app. This community build is ad-hoc signed, not Developer ID signed
   or notarized. If Gatekeeper blocks it, use Finder's **Open** action from the
   app's context menu and confirm that you trust the release source.

For the Installer package, open `BlazeAI.pkg` and follow the prompts. It
installs **Blaze AI.app** into `/Applications`. This package is not signed with
a Developer ID or notarized; macOS may require you to confirm that you trust
the release source.

From the ZIP download, extract the complete folder and move **Blaze AI.app** to
Applications. Open it using Finder. The same first-launch Gatekeeper note
applies.

## What works

- Open and close installed applications, type into the focused app, take
  screenshots, record the screen, and report system resources.
- Search the web in your browser, open supported media searches, and download a
  direct HTTP/HTTPS file URL to `Downloads/BlazeDownloads`.
- Load a compatible local model for general chat and computer-control tasks.
  Model weights are not included. Select a local `.gguf` file or Transformers
  model folder in Settings.
- Optional push-to-talk uses local speech recognition after its model is
  available on the device.

While Blaze is speaking, push and hold the mic button or press
**Ctrl+Shift+Space** to interrupt and start talking. The stop button or **Esc**
stops a generated or spoken reply. To reduce local response time, chat prompts
include the latest 8 messages and model replies are capped at 96 tokens; older
history stays saved in your chats.

Web search and downloads need an internet connection. Some messaging actions
prepare a draft; sending an iMessage requires a separate confirmation. App
availability and macOS privacy permissions affect which actions can run.

## Permissions

macOS may ask for Screen Recording, Microphone, Accessibility, or Automation
access when you use the related feature. Grant only the permissions needed for
your use in **System Settings → Privacy & Security**. Blaze does not listen for
a wake word; the microphone opens only when you start voice input.

## Safety and privacy

Agent mode asks before each computer action by default. Review the exact action
before approving it. Low-risk actions can be allowed for the task; shell, file
writes, keyboard/mouse input, clipboard writes, AppleScript, and ffmpeg always
need separate approval. Destructive shell, disk, power, encoded-script, and
AppleScript shell operations are blocked. Model-driven SSH and common tunnel or
port-forward commands are blocked. Keep **Ask before each action** enabled.
Downloaded files are not opened or run automatically.

Chats, settings, contacts, and model files stay in your macOS user profile.

## Requirements and known limits

- macOS 11 or later; Apple Silicon is recommended for local model inference.
- A local chat model and suitable memory are needed for general conversation and
  multi-step computer-control tasks. The offline butler commands work without
  one.
- Blaze cannot perform every task available to Siri or Google Assistant. Some
  apps have no automation interface, and some actions require user interaction.
- This release is not Developer ID signed or notarized. Gatekeeper may require
  you to explicitly open the app the first time.

## Build from source

Build on macOS:

```bash
./build_macos_dmg.sh --voice --models
```

Output: `dist/dmg/BlazeAI-<version>.dmg`. Local model weights are not bundled.
The GitHub Actions workflow attaches the DMG, installer package, app ZIP, and
README to a versioned GitHub Release.

For help, open an issue in the repository where you obtained this release.
