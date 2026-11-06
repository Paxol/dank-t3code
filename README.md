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
  - **Limits** shows each provider's remaining quota per window, its pace, the even-pace marker, reset times and banked reset credits. Paired with T3 Code, it streams the same data T3 Code's Usage page uses, including CLI Proxy pooled accounts. See [Live limits](#live-limits).

## Requirements

- DMS 1.5 or later.
- `python3` (3.10 or later), `xdg-open` and `notify-send`.
- For live limits: the Python `cryptography` package (`python-cryptography` on Arch), and `secret-tool` (libsecret) to remember the connection.
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

## Live limits

Without a connection, the Limits tab shows T3 Code's saved provider checks from `~/.t3/caches`. To get the same live data as T3 Code's Usage page, including CLI Proxy pooled accounts, pair the widget with the local T3 server once:

1. In T3 Code, open **Settings → Connections → Authorized clients** and click **Create link**.
2. Give it a label such as `T3 Code Bar Limits`. Under **Permissions**, keep only **View environment** (`orchestration:read`). The **Read only** preset includes more scopes.
3. Create the link and copy its code. Use the new code, not a provider key, an existing client token, or a full pairing URL.
4. In the widget, open **Usage → Limits → Connect…**, check the **Local port**, paste the code, and click **Pair & connect**.
5. Turn on **Remember in keyring** to reconnect automatically after a restart. Without it, the token lives only until the shell restarts.

How the connection works, following the macOS app:

- The code is exchanged at `http://127.0.0.1:<port>/oauth/token` for an access token with exactly `orchestration:read`. Only a DPoP-bound token is accepted, never a plain bearer token. The pairing code is never saved or logged.
- With that token the widget gets a WebSocket ticket, opens `ws://127.0.0.1:<port>/ws`, and subscribes to `subscribeServerConfig` with `usageLimitSources`. It reads provider snapshots and CLI Proxy sources, and ignores settings, keybinding and theme updates. It acknowledges every chunk and pings every 5 seconds.
- It only ever contacts `127.0.0.1`, with no proxies, cookies or redirects. It never sends refresh, reset-credit or settings commands.
- **Remember** stores only the widget's own token, its private key, port and expiry in the desktop keyring (`secret-tool`, service `t3codebar.limits`). **Disconnect** deletes it. To revoke the session on the server, use T3 Code's authorized-client controls. An expired token needs a new pairing.
- While connected, the live provider list is authoritative, so disabled or deleted providers are not revived from saved files. If the connection drops, the widget keeps the last live data, merges it with newer saved checks, says so, and reconnects every 3 seconds.
- DPoP limits reuse of a stolen token, but it does not authenticate the server: a process impersonating T3 Code on that port could receive the pairing code. Pair only while your local T3 Code is running.

## How it works

A daemon component runs `backend/t3bar.py watch` once for all bars and screens. The script opens T3 Code's `statev2.sqlite` read-only, derives each thread's status the way the original app does, and prints a JSON line whenever the inbox changes. It re-reads when the database or its WAL changes, and every 5 seconds regardless, because snooze timers and server liveness change without a write. The widget reads the shared snapshot and runs the script on demand for previews, usage and limits.

| Data | Source |
| --- | --- |
| Threads, statuses, previews | `statev2.sqlite`, opened read-only |
| Server online, browser link port | `server-runtime.json` (process presence, not an HTTP check) |
| Thread links | `environment-id` |
| Cost and tokens | `usage-scan-cache-vN.json`, `usage-model-rates.json`, and the `usagePriceOverrides` and `usageModelAliases` keys of `settings.json` |
| Limits, live | The local T3 server's `subscribeServerConfig` stream, after pairing |
| Limits, fallback | `~/.t3/caches/*.json`, T3 Code's saved provider snapshots |

The widget never writes to T3 Code's files and never reads T3 Code's credentials. Its only network traffic is the optional live connection to `127.0.0.1`. A second daemon process, `backend/t3live.py`, holds that connection and renders the Limits tab. Links open T3 Code with `t3code://threads/<environment>/<thread>`, or the browser with `http://127.0.0.1:<port>/<environment>/<thread>`.

The status rules, inbox order, usage counting, pricing and limits layout follow the original app, which documents them in its [compatibility notes](https://github.com/eliophan/t3code-bar/blob/main/docs/COMPATIBILITY.md). They depend on T3 Code's internal schema and cache formats, which can change between versions.

## Differences from the macOS app

- **Pairing is optional.** The macOS app requires the live connection before it shows anything. Here the thread list works without it, and the Limits tab falls back to saved provider checks until you pair.
- **Cost and Tokens read T3 Code's transcript cache only.** The local OpenCode and Antigravity databases, the Cursor account cache, and the Codex service-tier lookup in `statev2.sqlite` are not ported. Their usage is missing, and unresolved Codex turns count at standard speed.
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
python3 -I backend/t3live.py                  # live Limits process; reads JSON commands on stdin
dms ipc call plugins reload t3codeBar         # reload after a change to the root QML files
dms restart                                   # needed after changing other QML files, which Qt caches
dms ipc call widget toggle t3codeBar          # open or close the popout
```

`tests/canary` is the original project's recorded usage cache with the oracle's numbers for every period. The Python port matches all of them. Python 3.12+ compensates float error in `sum()`, so the backend adds floats left to right, as Swift and JavaScript do, to land on the same rounded cents.

## License

[MIT](LICENSE). Ported logic, artwork and test fixtures come from T3 Code Bar and T3 Code; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). T3 Code, OpenAI, Anthropic, Google, and xAI names and logos belong to their respective owners.
