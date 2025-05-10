"""
Gemini XMPP Bot
Created by Joseph Winkie
Licensed under AGPL 3.0 or later, with no warranty.
Copyright 2025
"""
import asyncio
import json
import re

import requests
import slixmpp
from google import genai
from google.genai import types
from markdown_it import MarkdownIt

# class HighlightRenderer(RendererHTML):
#     def render_token(self, tokens, idx, options, env):
#         token = tokens[idx]
#         if token.type == "fence" and token.info:
#             try:
#                 lexer = get_lexer_by_name(token.info.strip())
#                 formatter = HtmlFormatter()
#                 return highlight(token.content, lexer, formatter)
#             except Exception:
#                 pass
#         return super().render_token(tokens, idx, options, env)


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

with open("./login.json") as lf:
    login = json.load(lf)

client = genai.Client(api_key=login["gemini-api"])

chats = {}


def describe_from_url(muc: str, image_url: str) -> str:
    image = requests.get(image_url)
    content_type = image.headers.get('content-type', 'image/jpeg')  # fallback to jpeg if not found

    if content_type not in acceptable_formats:
        return ""

    chat = chats.get(muc)
    if chat is None:
        chat = client.chats.create(model="gemini-2.0-flash")
        chats[muc] = chat

    try:
        response = chat.send_message(
            [
                types.Part.from_bytes(
                    data=image.content,
                    mime_type=content_type,
                ),
                'Describe this image with as much detail as possible in 1 to 2 sentences'
            ]
        )
    except Exception as e:
        print(e)
        return str(e)

    print(response)

    return response.text


def respond_text(muc: str, body: str) -> str:
    key_body: str = body[len(login["displayname"]) + 2:]
    if key_body.startswith("forget"):
        if chats.get(muc) is not None:
            del (chats[muc])
        return "Drinking to forget! 🍻"
    elif key_body.startswith("help"):
        return """
Hi! I'm a bot that interacts with the Google Gemini Api for XMPP.
See my source code at https://github.com/jjj333-p/gemini-xmpp
        """

    # handles context
    chat = chats.get(muc)
    if chat is None:
        chat = client.chats.create(model="gemini-2.0-flash")
        chats[muc] = chat

    try:
        response = chat.send_message(body)
    except Exception as e:
        return str(e)
    return response.text


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

    async def start(self, event):

        await self.get_roster()
        self.send_presence()

        # join configured rooms
        for room in self.rooms:
            self.plugin['xep_0045'].join_muc(room, self.nick)

    async def muc_message(self, msg):

        # dont respond to self
        if msg['mucnick'] == self.nick:
            return

        # chat response
        if msg["body"].lower().startswith(self.nick.lower()):
            r = respond_text(msg["from"].bare, msg["body"])
            if r == "":
                r = "The llm refused to respond"

            # html encode and then convert to bytes
            html = md.render(r)
            r_bytes = html.encode("utf-8")

            # upload
            try:
                url = await self['xep_0363'].upload_file(
                    filename="o.html",
                    # domain=self.domain,
                    timeout=10,
                    input_file=r_bytes,
                    size=len(r_bytes),
                    content_type="text/html",
                )
            except Exception as e:
                url = str(e)

            print(url)

            if len(r) > 315:
                r = r[:300] + " { truncated }"

            # format quote
            rf = f"{msg['from'].resource}\n> {'> '.join(msg['body'].splitlines())}\n{r}\n{url}"

            self.send_message(
                mto=msg['from'].bare,
                mbody=rf,
                mtype='groupchat'
            )

            # self.plugin['xep_0461'].send_reply(
            #     reply_to=msg['from'],
            #     reply_id=msg['stanza-id'],
            #     fallback=rf,
            #     quoted_nick=msg["from"].resource,
            #     mto=msg['from'].bare,
            #     mbody=r,
            #     mtype='groupchat',
            # )

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
                desc = describe_from_url(msg['from'].bare, url)
                if desc != "":
                    rf = f"> {url}\n\n{desc}"
                    self.send_message(
                        mto=msg['from'].bare,
                        mbody=rf,
                        mtype='groupchat'
                    )

    def muc_online(self, presence):
        pass
        # if presence['muc']['nick'] != self.nick:
        #     print(f"User online: {presence['muc']['nick']} (Role: {presence['muc']['role']})")


if __name__ == '__main__':
    xmpp = MUCBot(login["jid"], login["password"], login["rooms"], login["displayname"])

    # Connect to the XMPP server and start processing XMPP stanzas.
    xmpp.connect()
    asyncio.get_event_loop().run_forever()
