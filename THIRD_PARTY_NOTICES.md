# Third-party notices

The gateway uses the official DingTalk Stream Python SDK, version 0.24.3:
https://github.com/open-dingtalk/dingtalk-stream-sdk-python

MIT License

Copyright (c) 2023 钉钉开放平台团队

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Design references

- Codex app-server protocol: https://learn.chatgpt.com/docs/app-server — native shared-thread control; existing terminals are not seized.
- DingTalk quoted-message correlation: https://github.com/soimy/openclaw-channel-dingtalk/blob/main/src/messaging/quoted-ref.ts — reference for originalProcessQueryKey. The bridge implements its own persistent exact mapping; it does not use timestamp guessing or bundle that source.
- DingTalk group send API: https://open.dingtalk.com/document/orgapp/the-robot-sends-a-group-message — processQueryKey receipts and message size budget.
- HAPI: https://github.com/tiann/hapi — design comparison for local/mobile shared sessions. No HAPI code is bundled.

- cc-connect: https://github.com/chenhg5/cc-connect — multi-CLI IM bridge architecture.
- claude-to-im-plus: https://github.com/JiangJingC/claude-to-im-plus — IM bridge comparison.
- cc-peer protocol notes: https://github.com/mikekelly/cc-peer/blob/main/docs/PROTOCOL.md — unofficial Claude peer wire protocol reference. No cc-peer source is bundled.
- Claude official cross-session documentation: https://code.claude.com/docs/en/cross-session-messaging
- Codex developer commands: https://learn.chatgpt.com/docs/developer-commands?surface=cli
- Official DingTalk card/Stream examples: https://github.com/open-dingtalk/dingtalk-card-examples and https://open-dingtalk.github.io/developerpedia/docs/explore/tutorials/stream/bot/go/send-streaming-card/ — StandardCard API structure and JSON-string private receiver.
- DingTalk emotion endpoint reference: https://github.com/DingTalk-Real-AI/dingtalk-openclaw-connector/blob/main/src/utils/utils-legacy.ts — text emotion reply/recall protocol; this project implements its own durable reaction worker and states.
- Claude official hook semantics: https://code.claude.com/docs/en/hooks — lifecycle observation; synchronous desktop hooks in this project return immediately without an approval decision.

This application is a new implementation using the DingTalk SDK; it is not a fork of cc-connect or claude-to-im-plus. CLI vendors do not endorse it. Claude peer wire details are version-sensitive. Unsupported sessions fail closed.

Other installed libraries retain their own license files in the Python environment, including requests (Apache-2.0), Paramiko (LGPL-2.1), and psutil (BSD-3-Clause). This repository does not vendor their source.
