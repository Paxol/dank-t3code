# T3 Code Bar for DMS

Your [T3 Code](https://github.com/pingdotgg/t3code) inbox in the Dux Modular Shell bar. See running threads, spot requests that need you, and jump back into a conversation with one click.

This is a Linux port of [eliophan/t3code-bar](https://github.com/eliophan/t3code-bar), a macOS menu bar app, rebuilt as a DMS bar widget. It is not affiliated with or endorsed by T3 Tools.

## Features

- **Bar mark.** The T3 mark with a dot in the color of the most urgent thread. When a thread needs you, a label such as `Approval 2` or `Failed` sits next to it. Otherwise the bar shows how many threads are running when there are several. Right-click the mark to open T3 Code.
- **Inbox.** Threads in T3 Code's sidebar order, with pinned threads first. Each row shows the project badge, title, branch, PR number, provider logos and a status: Working, Goal, Approval, Input, Request, Waiting, Failed, Limited (with its reset time), Woke, Done, or the time since the last message.
- **Previews.** Rest the pointer on a thread for half a second to read the start of its last assistant reply.
- **Actions.** Click a thread to open it in T3 Code. Right-click it for **Open in T3 Code**, **Open in Browser** and **Copy Link**. The header has **New thread** and **Open T3 Code**.
- **Notifications.** A desktop notification when a thread needs approval or input, gets a guest request, fails, or hits a usage limit.
- **Usage.** Click **Usage** under the list for the Cost, Tokens and Limits tabs of T3 Code's Usage page:
  - **Cost** and **Tokens** cover the past 24 hours, 7, 30 or 90 days, with cost or tokens by type, by provider, a daily (or hourly) chart, totals, cost by speed and a per-model breakdown.
  - **Limits** shows each provider's remaining quota per window, its pace, the even-pace marker, reset times and banked reset credits.
- **Live connection.** Paired with the local T3 server, the widget streams the thread list, limits (including CLI Proxy pooled accounts) and, optionally, cost and tokens from the same RPCs T3 Code's sidebar and Usage page use. Without it, everything is read from T3 Code's local files. See [Live connection](#live-connection).

## Requirements

- DMS 1.5 or later.
- `python3` (3.10 or later), `xdg-open` and `notify-send`.
- For the live connection: `secret-tool` (libsecret) to remember the connection.
- T3 Code, opened at least once. Its `t3code://` URL handler must be registered to open threads from the bar.

## Installation

```bash
git clone <this repository> ~/.config/dms/plugins/t3codeBar
dms ipc call plugin-scan scan
dms ipc call plugins enable t3codeBar
```

Then add **T3 Code Bar** to a bar section in **Settings → Dank Bar**, or open **Settings → Plugins**, scan, and enable it there.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| Status label in the bar | On | Shows the status label or running count next to the mark. |
| Hide when the inbox is empty | Off | Removes the widget from the bar when no thread is active. |
| Show previews | On | Hover previews. They show real message text, so turn them off when you share your screen. |
| Notifications | On | Desktop notifications for threads that need attention. |
| T3 Code status colors | On | T3 Code's own status colors. Turn off to use the shell theme. |
| 24-hour clock | On | Reset times on the Limits tab. |
| Refresh interval | 1 second | How often the database is checked for changes. |
| Popout height | Normal | The height after which the list scrolls. |
| T3 Code data directory | empty | Folder with `statev2.sqlite`. Empty uses `$T3_CODE_BAR_STATE_DIR`, then `$T3CODE_HOME/userdata`, then `~/.t3/userdata`. |
| T3 Code desktop entry | `com.t3tools.T3Code` | The desktop entry that **Open T3 Code** launches. |

## Live connection

Without a connection, the widget reads T3 Code's files: threads from `statev2.sqlite`, cost and tokens from the transcript cache, and limits from the saved provider checks in `~/.t3/caches`. To get the same live data as T3 Code itself, connect the widget to the local T3 server once, with a pairing code or an access token.

With a pairing code (the token lasts 30 days):

1. In T3 Code, open **Settings → Connections → Authorized clients** and click **Create link**.
2. Give it a label such as `T3 Code Bar`. Under **Permissions**, keep only **View environment** (`orchestration:read`) and, for live cost and tokens, **View diagnostics and usage** (`diagnostics:read`). The **Read only** preset includes more scopes.
3. Create the link and copy its code. Use the new code, not a provider key or a full pairing URL.

With an access token from T3 Code's CLI, for a lifetime you choose:

```bash
t3 auth session issue --scope orchestration:read --scope diagnostics:read --ttl 365d --label "T3 Code Bar" --token-only
```

Then:

1. In the widget, open **Usage → Limits → Connect…**, check the **Local port**, paste the code or the token, and click **Connect**.
2. Turn on **Remember in keyring** to reconnect automatically after a restart. Without it, the token lives only until the shell restarts.

What each permission turns live:

| Permission | Live data | RPC |
| --- | --- | --- |
| View environment | Thread list and statuses | `orchestration.subscribeShell` |
| View environment | Limits, including CLI Proxy pooled accounts | `subscribeServerConfig` with `usageLimitSources` |
| View diagnostics and usage | Cost and Tokens tabs | `server.getUsageSummary` |

A code with only **View environment** works as before: threads and limits are live, and cost and tokens stay on the transcript cache. A token remembered before usage was live is treated the same way; connect again with both permissions for live usage.

How the connection works, following the macOS app:

- A pairing code is exchanged at `http://127.0.0.1:<port>/oauth/token` for a bearer access token. The widget asks for `orchestration:read diagnostics:read`; the server grants the ones the code allows. It sends no DPoP proof, as the current upstream app does: T3 Code gives a DPoP-bound token one hour and no refresh, and a bearer token the session's default 30 days. A token with any scope beyond those two is refused, whether exchanged or pasted. The pairing code is never saved or logged.
- A pasted access token (anything with a `.`, as T3's session tokens have) is used as it is. Its expiry and scopes are read from its claims.
- With the token, sent as `Authorization: Bearer`, the widget gets a WebSocket ticket, opens `ws://127.0.0.1:<port>/ws`, and subscribes to `subscribeServerConfig` with `usageLimitSources`. It reads provider snapshots and CLI Proxy sources, and ignores settings, keybinding and theme updates. After the first config it subscribes to `orchestration.subscribeShell`, leaving out idle subagents and older settled threads when the server supports it. It acknowledges every chunk and pings every 5 seconds.
- The thread shells go through the same status rules and inbox order as the `statev2.sqlite` reader. Previews still read the last reply from `statev2.sqlite`.
- The Cost and Tokens tabs ask `server.getUsageSummary` for the period, with the same window T3 Code's Usage page sends (the past 24 hours by hour, otherwise by day in the local time zone). The server scans and prices the transcripts itself, so live numbers include every source T3 Code reads. A failed or slow request falls back to the transcript cache and says why.
- It only ever contacts `127.0.0.1`, with no proxies, cookies or redirects. It never sends refresh, reset-credit, settings or thread commands.
- **Remember** stores only the widget's own token, its port, scopes and expiry in the desktop keyring (`secret-tool`, service `t3codebar.limits`). **Disconnect** deletes it. To revoke the session on the server, use T3 Code's authorized-client controls. Neither kind of token can be refreshed: when T3 Code rejects one as expired or revoked, the widget says so and deletes it from the keyring, and you connect again with a new code or token. A token remembered by the DPoP version is deleted the same way.
- The widget follows `server-runtime.json`. While T3 Code is not running the connection pauses; when it starts again, on the same or a new port, a remembered token reconnects.
- While connected, the live provider list is authoritative, so disabled or deleted providers are not revived from saved files. If the connection drops, the thread list returns to `statev2.sqlite`, the Limits tab keeps the last live data, merges it with newer saved checks and says so, and the widget reconnects every 3 seconds.
- A bearer token works for whoever holds it. It is limited to reading this machine's T3 Code, and stored only in the keyring. Nothing authenticates the server either: a process impersonating T3 Code on that port could receive the code or token. Connect only while your local T3 Code is running.

## How it works

A daemon component runs `backend/t3bar.py watch` once for all bars and screens. The script opens T3 Code's `statev2.sqlite` read-only, derives each thread's status the way the original app does, and prints a JSON line whenever the inbox changes. It re-reads when the database or its WAL changes, and every 5 seconds regardless, because snooze timers and server liveness change without a write. The widget reads the shared snapshot and runs the script on demand for previews, usage and limits.

The daemon also runs `backend/t3live.py`, which holds the optional live connection. While it is live, its thread inbox replaces the `statev2.sqlite` one, and the Usage screen sends it usage requests instead of reading the cache.

| Data | Source |
| --- | --- |
| Threads and statuses, live | The local T3 server's `orchestration.subscribeShell` stream, after pairing |
| Threads and statuses, fallback; previews | `statev2.sqlite`, opened read-only |
| Server online, browser link port | `server-runtime.json` (process presence, not an HTTP check) |
| Thread links | `environment-id` |
| Cost and tokens, live | The local T3 server's `server.getUsageSummary`, after pairing with View diagnostics and usage |
| Cost and tokens, fallback | `usage-scan-cache-vN.json`, `usage-model-rates.json`, and the `usagePriceOverrides` and `usageModelAliases` keys of `settings.json` |
| Limits, live | The local T3 server's `subscribeServerConfig` stream, after pairing |
| Limits, fallback | `~/.t3/caches/*.json`, T3 Code's saved provider snapshots |

The widget never writes to T3 Code's files and never reads T3 Code's credentials. Its only network traffic is the optional live connection to `127.0.0.1`. Links open T3 Code with `t3code://threads/<environment>/<thread>`, or the browser with `http://127.0.0.1:<port>/<environment>/<thread>`.

The status rules, inbox order, usage counting, pricing and limits layout follow the original app, which documents them in its [compatibility notes](https://github.com/eliophan/t3code-bar/blob/main/docs/COMPATIBILITY.md). They depend on T3 Code's internal schema and cache formats, which can change between versions.

## Differences from the macOS app

- **Pairing is optional.** The macOS app requires the live connection for limits. Here every tab works without it, from T3 Code's files, and pairing makes threads, limits and, with the usage permission, cost and tokens live.
- **Threads and usage can come from the server.** The macOS app reads them from `statev2.sqlite` and the transcript cache only. The live thread shell reports one pending request per thread rather than a count, and a failed latest run stands for the provider session's error state, which only matters when a snoozed thread fails.
- **Without live usage, Cost and Tokens read T3 Code's transcript cache only.** The local OpenCode and Antigravity databases, the Cursor account cache, and the Codex service-tier lookup in `statev2.sqlite` are not ported. Their usage is missing, and unresolved Codex turns count at standard speed. Live usage has none of these gaps, because the server does the scan.
- **No self-updater.** Update with `git pull` in the plugin folder, then run `dms ipc call plugins reload t3codeBar`.
- Status icons are Material Symbols from the shell instead of Lucide icons.

## Development

```bash
python3 -I backend/t3bar.py threads           # one inbox snapshot
python3 -I backend/t3bar.py preview <thread>  # start of the last reply
python3 -I backend/t3bar.py limits            # the Limits tab
python3 -I backend/t3bar.py usage --period 7  # the Cost and Tokens tabs
python3 -I tests/test_usage_canary.py         # usage numbers against the original's golden canary
python3 -I tests/test_live.py                 # live connection against a fake T3 server
python3 -I backend/t3live.py                  # live connection process; reads JSON commands on stdin
dms ipc call plugins reload t3codeBar         # reload after a change to the root QML files
dms restart                                   # needed after changing other QML files, which Qt caches
dms ipc call widget toggle t3codeBar          # open or close the popout
```

`tests/canary` is the original project's recorded usage cache with the oracle's numbers for every period. The Python port matches all of them. Python 3.12+ compensates float error in `sum()`, so the backend adds floats left to right, as Swift and JavaScript do, to land on the same rounded cents.

## License

[MIT](LICENSE). Ported logic, artwork and test fixtures come from T3 Code Bar and T3 Code; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). T3 Code, OpenAI, Anthropic, Google, and xAI names and logos belong to their respective owners.
