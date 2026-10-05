"""
Gemini XMPP Bot
Created by Joseph Winkie
Licensed under AGPL 3.0 or later, with no warranty.
Copyright 2025
"""
import asyncio
import base64
import json
import re
from typing import AsyncGenerator

import aiohttp
import slixmpp
from google import genai
from google.genai import types
from markdown_it import MarkdownIt

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

client = genai.Client(api_key=login.get("gemini-api", ""))

chats = {}

chat_lock: dict[str, asyncio.Lock] = {}


async def describe_from_bytes(muc: str, image_content: bytes, content_type: str) -> str:
    chat = chats.get(muc)
    if chat is None:
        chat = client.chats.create(model=login['gemini-model'])
        chats[muc] = chat

    response = await asyncio.to_thread(
        chat.send_message,
        [
            types.Part.from_bytes(
                data=image_content,
                mime_type=content_type,
            ),
            'Describe this image with as much detail as possible in 1 to 2 sentences'
        ]
    ),

    return "\n".join(part.text for part in response)


async def describe_from_url(muc: str, image_url: str) -> str:
    print("trying to describe from url", image_url)

    async with aiohttp.ClientSession() as session:
        async with session.get(image_url) as response:
            content_type = response.headers.get(
                'content-type', 'image/jpeg')  # fallback to jpeg if not found
            if content_type not in acceptable_formats:
                return ""
            content_length = int(
                response.headers.get('content-length', '0'))
            if content_length > max_file_len:
                return f"File too large ({content_length} bytes > {max_file_len} bytes)"

            image_content = await response.read()

    return await describe_from_bytes(muc, image_content, content_type)


async def respond_text(muc: str, body: str) -> str:
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
        chat = client.chats.create(model=login['gemini-model'])
        chats[muc] = chat

    try:
        response = await asyncio.to_thread(chat.send_message, body)
    except Exception as e:
        return str(e)
    return response.text


async def post_to_pinnwand(content: str) -> str:
    """Post markdown content to pinnwand instance with 1 week expiry."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
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


async def generate_image(prompt: str) -> AsyncGenerator[bytes, None]:
    print(f"Generating image \"{prompt}\"")

    headers = {"x-api-key": login["nanogpt-api"]}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
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
        # stanza is received from any chat room. If you also also
        # register a handler for the 'message' event, MUC messages
        # will be processed by both handlers.
        self.add_event_handler("groupchat_message", self.muc_message)

        # The groupchat_presence event is triggered whenever a
        # presence stanza is received from any chat room, including
        # any presences you send yourself. To limit event handling
        # to a single room, use the events muc::room@server::presence,
        # muc::room@server::got_online, or muc::room@server::got_offline.
        self.add_event_handler("muc::%s::got_online" % self.rooms[0],
                               self.muc_online)

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

        async with lock:

            # chat response
            if msg["body"].lower().startswith(self.nick.lower()):
                r = await respond_text(msg["from"].bare, msg["body"])
                if r == "":
                    r = "The llm refused to respond"

                if len(r) > 315:
                    # post to pinnwand
                    url = await post_to_pinnwand(r)

                    print(url)

                    r = r[:300] + " { truncated } \n" + url

                # format quote
                rf = f"{msg['from'].resource}\n> {'> '.join(msg['body'].splitlines())}"

                message: slixmpp.stanza.Message = self['xep_0461'].make_reply(
                    msg['from'],
                    msg['stanza_id']['id'],
                    rf,
                    mto=msg['from'].bare,
                    mbody=r,
                    mtype='groupchat'
                )
                message.send()

            elif msg["body"].lower().startswith(login["nanogpt-image-model"]):
                image_generated = False
                async for img_bytes in generate_image(
                        msg["body"][len(login["nanogpt-image-model"]) + 1:]
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

                    desc = await describe_from_bytes(msg['from'].bare, img_bytes, "image/jpeg")
                    if desc != "":
                        rf = f"> {url}\n{desc}"
                        self.send_message(
                            mto=msg['from'].bare,
                            mbody=rf,
                            mtype='groupchat'
                        )

                if not image_generated:
                    parse_body = msg['body'].split('\n').join('\n> ')
                    message: slixmpp.stanza.Message = self['xep_0461'].make_reply(
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
                        desc = await describe_from_url(msg['from'].bare, url)
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
                        message: slixmpp.stanza.Message = self['xep_0461'].make_reply(
                            msg['from'],
                            msg['stanza_id']['id'],
                            url,
                            mto=msg['from'].bare,
                            mbody=desc,
                            mtype='groupchat'
                        )
                        message.send()

    def muc_online(self, presence):
        pass
        # if presence['muc']['nick'] != self.nick:
        #     print(f"User online: {presence['muc']['nick']} (Role: {presence['muc']['role']})")


if __name__ == '__main__':
    xmpp = MUCBot(login["jid"], login["password"],
                  login["rooms"], login["displayname"])

    # Connect to the XMPP server and start processing XMPP stanzas.
    xmpp.connect()
    print("Connected and running forever...")
    asyncio.get_event_loop().run_forever()
