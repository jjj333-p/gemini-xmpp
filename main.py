"""
Gemini XMPP Bot
Created by Joseph Winkie
Licensed under AGPL 3.0 or later, with no warranty.
Copyright 2025
"""
import asyncio
import base64
import ipaddress
import json
import re
import socket
from collections.abc import Iterable
from types import CoroutineType
from typing import AsyncGenerator, Callable, Any
from urllib.parse import urlparse

import aiohttp
import slixmpp
from google import genai
from google.genai import types
from google.genai.chats import AsyncChat
from markdown_it import MarkdownIt
from slixmpp import JID

md = MarkdownIt()  # (renderer_cls=HighlightRenderer)

# formats compat with gemini image comprehension
acceptable_formats: list[str] = [
    "image/png",
    "image/jpg",
    "image/jpeg",
    "image/webp",
    "image/heic",
    "image/heif"
]

url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'

with open("./login.json", encoding="utf-8") as lf:
    login = json.load(lf)

max_file_len: int = int(login.get("max_file_len", 10)) * \
                    1024 * 1024  # 5MB max file size

pinnwand_url: str = login.get("pinnwand_url", "https://pussybin.com")

gemini_client = genai.Client(api_key=login.get("gemini-api", ""))

chats = {}

chat_lock: dict[str, asyncio.Lock] = {}

aiohttp_headers = {
    "User-Agent": "GeminiXMPPBot/1.0 (+https://github.com/jjj333-p/gemini-xmpp)"
}

aiohttp_session: aiohttp.ClientSession | None = None

# Pre-compiled regex patterns
URL_RE = re.compile(r'https?://\S+')
STRIP_PUNCT_RE = re.compile(r'[)>\]\.]+$')


def is_safe_public_url(url: str) -> bool:
    """Verifies that the target host resolves exclusively to public, routable IP addresses."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False

        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addr_info = socket.getaddrinfo(parsed.hostname, port)

        for _, _, _, _, sockaddr in addr_info:
            ip = ipaddress.ip_address(sockaddr[0])
            if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_multicast
                    or ip.is_reserved
            ):
                return False
        return True
    except Exception:
        return False


async def expand_pastebin_links(body: str) -> str:
    """Finds pastebin URLs at the end of truncated messages, validates them, and expands content."""
    lines = [line.rstrip() for line in body.splitlines() if line.strip()]
    if len(lines) < 2:
        return body

    last_line = lines[-1]
    url_match = URL_RE.search(last_line)
    if not url_match:
        return body

    url = STRIP_PUNCT_RE.sub('', url_match.group(0))

    if not is_safe_public_url(url):
        return body

    print("expanding pastebin url", url)

    try:
        async with aiohttp_session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status == 200:
                content_type = resp.headers.get("content-type", "").lower()
                # mod_pastebin and most raw paste views return text/plain
                if "text/plain" in content_type:
                    paste_text = await resp.text()

                    # Verify that the preview is indeed a prefix of the paste
                    preview = "\n".join(lines[:-1]).rstrip(". ")
                    # Match up to 60 chars to avoid trailing ellipsis/whitespace variations
                    check_slice = preview[:min(len(preview), 60)].strip()
                    if check_slice and paste_text.startswith(check_slice):
                        return paste_text
    except Exception as e:
        print(f"Paste expansion skipped for {url}: {e}")

    return body


MAX_TOOL_BYTES = 64 * 1024  # 64 KB is plenty for ~20,000 characters


def make_fetch_webpage(client, muc_jid: JID) -> Callable[..., CoroutineType[Any, Any, str]]:
    async def fetch_webpage(url: str) -> str:
        """
        Fetches text content from a web page or pastebin URL.

        :param url: The web URL to inspect.
        """

        print("LLM is attempting to fetch for", muc_jid, url)

        if not is_safe_public_url(url):
            return "Access denied: Target URL is invalid, private, or not a routable public address."

        if aiohttp_session is None:
            return "HTTP client session is not active."

        # boilerplate message obj
        message = client.make_message(
            mto=muc_jid,
            mbody=f"/me is fetching the url `{url}`",
            mtype='groupchat'
        )

        message.send()

        try:
            async with aiohttp_session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Failed to fetch content, HTTP status {resp.status}")

                content_type = resp.headers.get("content-type", "").lower()
                if "text" not in content_type and "json" not in content_type:
                    raise RuntimeError(f"Unsupported content type: {content_type}")

                # Pull at most MAX_TOOL_BYTES from the network
                raw_bytes = await resp.content.read(MAX_TOOL_BYTES)

                encoding = resp.get_encoding() or "utf-8"
                text = raw_bytes.decode(encoding, errors="replace")

                # Check if there was more left unread or if text exceeds token budget
                was_truncated = not resp.content.at_eof() or len(text) > 20000

                truncated_text = text[:20000]
                if was_truncated:
                    truncated_text += "\n\n[Warning: Content truncated due to length limits.]"

                return truncated_text
        except Exception as e:
            e_str = f"Error fetching URL: {e}"

            # boilerplate message obj
            message = client.make_message(
                mto=muc_jid,
                mbody=e_str,
                mtype='groupchat'
            )
            message.send()

            return e_str

    return fetch_webpage


def make_send_file(xmpp_client, muc_jid: JID) -> Callable[[str], None]:
    def send_file(url: str):
        """
        Send any url as an attachment to the XMPP muc.
        Use if there is any relevant media you would like to show the user.
        You do *not* need to reattach media the user has sent you.
        :param url: the url of the attachment
        """
        # boilerplate message obj
        message = xmpp_client.make_message(
            mto=muc_jid,
            mbody=url,
            mtype='groupchat'
        )

        # attach media tag
        # pylint: disable=invalid-sequence-index
        message['oob']['url'] = url
        message.send()

    return send_file


def create_gemini_chat(xmpp_client, muc_jid: JID) -> AsyncChat:
    return gemini_client.aio.chats.create(
        model=login['gemini-model'],
        config=types.GenerateContentConfig(
            tools=[
                make_fetch_webpage(xmpp_client, muc_jid),
                make_send_file(xmpp_client, muc_jid)
            ],
            system_instruction=(
                "You are an XMPP chat bot. You can fetch URLs or pastebins if a message "
                "refers to external links or appears truncated, or it otherwise seems relevant."
                "feel free to share any information or errors you have, as you do not have access to sensitive information."
            )
        )
    )


async def describe_from_bytes(
        client, muc: JID,
        image_content: bytes, content_type: str,
        msg_body: str, msg_from: str = "<unknown>"
) -> str:
    chat = chats.get(muc)
    if chat is None:
        chat = create_gemini_chat(client, muc)
        chats[muc] = chat

    response = await chat.send_message(
        [
            types.Part.from_bytes(
                data=image_content,
                mime_type=content_type,
            ),
            f'Describe this image from {msg_from} with as much detail as possible in 1 to 2 sentences\n\nmessage body (any relevant media is already attached):\n{msg_body}'
        ]
    )

    return "\n".join(part.text for part in response.candidates[0].content.parts if part.text)


async def describe_from_url(client, muc: JID, image_url: str, msg_body: str, msg_from: str = "<unknown>") -> str:
    print("trying to describe from url", image_url)

    if aiohttp_session is None:
        print("no aiohttp session")
        return "no aiohttp session"

    async with aiohttp_session.get(image_url) as response:
        content_type = response.headers.get(
            'content-type', 'image/jpeg')  # fallback to jpeg if not found
        if content_type not in acceptable_formats:
            return ""
        content_length = int(
            response.headers.get('content-length', '0'))
        if content_length > max_file_len:
            return f"File too large ({content_length} bytes > {max_file_len} bytes)"

        image_content = await response.read()

    return await describe_from_bytes(client, muc, image_content, content_type, msg_body, msg_from)


async def respond_text(client, muc: JID, body: str, msg_from: str = "<unknown>") -> str:
    key_body: str = body[len(login["displayname"]) + 2:]
    if key_body.startswith("forget"):
        if chats.get(muc) is not None:
            del chats[muc]
        return "Drinking to forget! 🍻"
    elif key_body.startswith("help"):
        return """
Hi! I'm a bot that interacts with the Google Gemini Api for XMPP.
See my source code at https://github.com/jjj333-p/gemini-xmpp
        """

    # handles context
    chat = chats.get(muc)
    if chat is None:
        chat = create_gemini_chat(client, muc)
        chats[muc] = chat

    try:
        response = await chat.send_message(f"Message from {msg_from}:\n{body}")
    except Exception as e:
        return str(e)
    return response.text or ""


async def post_to_pinnwand(content: str) -> str:
    """Post markdown content to pinnwand instance with 1 week expiry."""
    try:
        async with aiohttp_session.post(
                f"{pinnwand_url}/api/v1/paste",
                json={
                    "expiry": "1 week",
                    "files": [
                        {
                            "lexer": "markdown",
                            "content": content
                        }
                    ]
                }
        ) as response:
            result = await response.json()
            print(result)
            return result.get("link", "")
    except Exception as e:
        return str(e)


# noinspection bad-index
def remove_fallbacks(body: str, namespaces: Iterable[str | None], fallbacks: Iterable[str]) -> str:
    # get out fallback ranges for features we support
    ranges_to_remove = []
    for fallback in fallbacks:
        # slixmpp stanza interface should never return None, so all None objects are effectively ignored
        if fallback["for"] in namespaces:
            start = int(fallback["body"]["start"])
            end = int(fallback["body"]["end"])
            ranges_to_remove.append((start, end))
    # Remove in reverse order so indices stay valid
    ranges_to_remove.sort(reverse=True)
    result = list(body)
    for start, end in ranges_to_remove:
        if end > len(result):
            continue
        if start < 0:
            continue
        del result[start:end]

    return "".join(result).strip()


async def generate_image(prompt: str) -> AsyncGenerator[bytes, None]:
    print(f"Generating image \"{prompt}\"")

    headers = {"x-api-key": login["nanogpt-api"]}

    try:
        async with aiohttp_session.post(
                "https://nano-gpt.com/api/generate-image",
                headers=headers,
                json={
                    "model": login["nanogpt-image-model"],
                    "prompt": prompt,
                    "width": login["nanogpt-image-w"],
                    "height": login["nanogpt-image-h"],
                }
        ) as response:
            result = await response.json()
            for b64 in result.get("data", []):
                b64_data = b64.get("b64_json")
                if b64_data is None:
                    continue
                yield base64.b64decode(b64_data)
    except Exception as e:
        print(e)


class MUCBot(slixmpp.ClientXMPP):

    def __init__(self, jid, password, rooms, nick):
        slixmpp.ClientXMPP.__init__(self, jid, password)

        self.rooms = rooms
        self.nick = nick

        self.add_event_handler("session_start", self.start)

        # The groupchat_message event is triggered whenever a message
        # stanza is received from any MUC.
        self.add_event_handler("groupchat_message", self.muc_message)

        self.register_plugin('xep_0030')  # Service Discovery
        self.register_plugin('xep_0045')  # Multi-User Chat
        self.register_plugin('xep_0199')  # XMPP Ping
        self.register_plugin('xep_0461')  # Message Replies
        self.register_plugin('xep_0363')  # HTTP file upload
        self.register_plugin('xep_0066')  # OOB file embed
        self.register_plugin('xep_0359')  # (Unique and Stable Stanza IDs)
        self.register_plugin('xep_0444')  # Message Reactions

    async def start(self, _):
        await self.get_roster()
        self.send_presence()

        # join configured rooms
        for room in self.rooms:
            await self.plugin['xep_0045'].join_muc(room, self.nick)

    async def muc_message(self, msg):
        # dont respond to self
        if msg['mucnick'] == self.nick:
            return

        lock: asyncio.Lock | None = chat_lock.get(msg['from'].bare)
        if lock is None:
            lock: asyncio.Lock = asyncio.Lock()
            chat_lock[msg['from'].bare] = lock

        cleaned_body: str = remove_fallbacks(
            msg["body"],
            ("urn:xmpp:reply:0",),
            msg['fallbacks']
        ).lower()

        expanded_body = await expand_pastebin_links(msg["body"])

        async with lock:

            # chat response
            if cleaned_body.startswith(self.nick.lower()):
                r = (
                        await respond_text(
                            self, msg["from"].bare, expanded_body,
                            msg['from'].resource
                        )
                    ) or "The llm refused to respond"

                if len(r) > 315:
                    # post to pinnwand
                    url = await post_to_pinnwand(r)

                    print(url)

                    r = r[:300] + " { truncated } \n" + url

                # format quote
                rf = f"{msg['from'].resource}\n> {'> '.join(msg['body'].splitlines())}"

                message: slixmpp.stanza.Message = self.plugin['xep_0461'].make_reply(
                    msg['from'],
                    msg['stanza_id']['id'],
                    rf,
                    mto=msg['from'].bare,
                    mbody=r,
                    mtype='groupchat'
                )
                message.send()

            elif cleaned_body.startswith(login["nanogpt-image-model"]):
                image_generated = False
                async for img_bytes in generate_image(
                        expanded_body[len(login["nanogpt-image-model"]) + 1:]
                ):
                    image_generated = True

                    # upload
                    try:
                        url = await self['xep_0363'].upload_file(
                            filename="generated.jpeg",
                            # domain=self.domain,
                            timeout=10,
                            input_file=img_bytes,
                            size=len(img_bytes),
                            content_type="image/jpeg",
                        )
                    except Exception as e:
                        url = str(e)

                    print(url)

                    # boilerplate message obj
                    message = self.make_message(
                        mto=msg['from'].bare,
                        mbody=url,
                        mtype='groupchat'
                    )

                    # attach media tag
                    # pylint: disable=invalid-sequence-index
                    message['oob']['url'] = url
                    message.send()

                    desc = await describe_from_bytes(self, msg['from'].bare, img_bytes, "image/jpeg", "")
                    if desc != "":
                        rf = f"> {url}\n{desc}"
                        self.send_message(
                            mto=msg['from'].bare,
                            mbody=rf,
                            mtype='groupchat'
                        )

                if not image_generated:
                    parse_body = msg['body'].split('\n').join('\n> ')
                    message: slixmpp.stanza.Message = self.plugin['xep_0461'].make_reply(
                        msg['from'],
                        msg['stanza_id']['id'],
                        f"> {parse_body}",
                        mto=msg['from'].bare,
                        mbody=f"Failed to generate any images for prompt {msg['body']}",
                        mtype='groupchat'
                    )
                    message.send()

            urls_found = []
            for line in msg["body"].splitlines():
                # filter out replies
                if line.startswith(">"):
                    continue
                for url in re.findall(url_pattern, line):
                    # dont parse a url several times
                    if url in urls_found:
                        continue
                    urls_found.append(url)

                    # generate description
                    try:
                        desc = await describe_from_url(
                            self, msg['from'].bare, url,
                            msg['body'].replace(url, "<raw url of attached media>"),
                            msg['from'].resource
                        )
                    except Exception as e:
                        print(e)
                        reaction_msg = self.make_message(
                            mto=msg['from'].bare,
                            mtype='groupchat'
                        )
                        self.plugin['xep_0444'].set_reactions(
                            reaction_msg,
                            msg['stanza_id']['id'],
                            '🤷'
                        )
                        reaction_msg.send()
                        continue

                    if desc != "":
                        message: slixmpp.stanza.Message = self.plugin['xep_0461'].make_reply(
                            msg['from'],
                            msg['stanza_id']['id'],
                            url,
                            mto=msg['from'].bare,
                            mbody=desc,
                            mtype='groupchat'
                        )
                        message.send()


if __name__ == '__main__':
    xmpp = MUCBot(login["jid"], login["password"],
                  login["rooms"], login["displayname"])

    aiohttp_session = aiohttp.ClientSession(
        headers=aiohttp_headers,
        loop=asyncio.get_event_loop()
    )

    # Connect to the XMPP server and start processing XMPP stanzas.
    xmpp.connect()
    print("Connected and running forever...")
    asyncio.get_event_loop().run_forever()
