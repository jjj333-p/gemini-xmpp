# Gemini XMPP Bot

This is just a little bot I made, honestly just wanted alt text to describe images when I cant open them in public
(for moderation), but I figured while I was plumbing in gemini I might as well let you chat with it and discuss the
images too.

### Use

- It will scan every url found, and respond to the ones that are supported images
- messages starting with its displayname it will respond to.
- Messages starting with the image generation model (`hidream` on this instance) will generate an image
- use `{displayname}, forget` to clear context window
- use `{displayname}, help` to get back here

### Login details

```json
{
  "gemini-api": "[REDACTED]"
```

API token for gemini, as per the gemini api docs

```
    "nanogpt-api": "[REDACTED]",
```

API token for NanoGPT integration

```
    "nanogpt-image-model": "hidream",
```

The model name to use for NanoGPT image generation

```
    "nanogpt-image-w": 1360,
    "nanogpt-image-h": 768,
```

Width and height settings for generated images (in pixels)

```
    "max_file_len": 10
```

Maximum file size (in megabytes) of the images to download and send to the gemini api

```
    "jid": "gemini@pain.agency",
```

JID (string) with localpart and server for the bot

```
    "password": "[REDACTED]",
    "displayname": "gemini",
```

Password and Displayname for the bot's login

```
    "rooms": [
        "testing@group.pain.agency",
        "chaos@group.pain.agency",
    ]
}
```

List of rooms/mucs to join on startup