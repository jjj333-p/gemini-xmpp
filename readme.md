# Gemini XMPP Bot

This is just a little bot I made, honestly just wanted alt text to describe images when I cant open them in public
(for moderation), but I figured while I was plumbing in gemini I might as well let you chat with it and discuss the
images too.

### Use

- It will scan every url found, and respond to the ones that are supported images
- images starting with its displayname it will respond to
- use `{displayname}, forget` to clear context window
- use `{displayname}, help` to get back here

### Login details

```json
{
  "gemini-api": "[REDACTED]"
```

API token for gemini, as per the gemini api docs

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