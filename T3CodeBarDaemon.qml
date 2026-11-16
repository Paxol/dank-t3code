import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Services
import qs.Modules.Plugins
import "T3Fmt.js" as Fmt

// Runs `backend/t3bar.py watch` once for every bar and screen, publishes each
// inbox snapshot as the "inbox" global var, and raises desktop notifications
// when a thread starts needing attention.
//
// It also runs `backend/t3live.py`, the live connection to the local T3 server.
// That process renders the Limits tab ("limitsLive"), streams the thread inbox,
// which replaces the statev2.sqlite reading while it is live, and answers usage
// requests ("usageLive"). The widget sends it commands through "liveCommand".
//
// The same process connects to paired remote environments. Their threads join
// the inbox for the selected environment, or for all of them, and so do their
// notifications. With no remote environment the inbox is the local one, as before.
PluginComponent {
    id: root

    readonly property string scriptPath: decodeURIComponent(Qt.resolvedUrl("backend/t3bar.py").toString().replace("file://", ""))
    readonly property string stateDir: (pluginData.stateDir || "").trim()
    readonly property real pollSeconds: Math.max(0.5, Number(pluginData.pollSeconds) || 1)
    readonly property string liveScriptPath: decodeURIComponent(Qt.resolvedUrl("backend/t3live.py").toString().replace("file://", ""))
    readonly property bool clock24: pluginData.clock24 !== undefined ? pluginData.clock24 : true
    property int liveRestarts: 0
    readonly property bool notificationsEnabled: pluginData.notifications !== undefined ? pluginData.notifications : true

    // Statuses that raise a notification when a thread enters them (AttentionTracker).
    readonly property var alertingStatuses: ["approval", "input", "request", "failed", "limited"]
    property var knownStatuses: null
    property int restarts: 0
    property bool manualRestart: false

    // The inbox from statev2.sqlite, and the one streamed by the live connection, which wins.
    property var localInbox: null
    property var liveInbox: null
    property string inboxSource: ""

    // Remote environments from the live process, and which environment the widget shows:
    // "all", or one environment ID. A selected environment that disappears stays selected.
    property var remoteState: null
    readonly property string selection: (pluginData.environment || "all")
    property var envSessions: ({})
    property bool selectionSent: false

    onSelectionChanged: {
        root.knownStatuses = null;
        root.sendSelection();
        root.publishInbox();
    }

    function sendSelection() {
        if (liveProcess.running)
            liveProcess.write(JSON.stringify({
                "cmd": "select",
                "environment": root.selection
            }) + "\n");
    }

    function command() {
        const cmd = ["python3", "-I", root.scriptPath];
        if (root.stateDir)
            cmd.push("--state-dir", root.stateDir);
        cmd.push("watch", "--interval", String(root.pollSeconds));
        return cmd;
    }

    function publish(snapshot) {
        if (pluginService)
            pluginService.setGlobalVar(pluginId, "inbox", snapshot);
    }

    // A switch between the two sources starts a new alert baseline, so the small
    // differences between them do not raise notifications.
    function publishInbox() {
        const snapshot = root.composeInbox();
        if (!snapshot)
            return;
        const source = root.liveInbox ? "live" : "local";
        if (source !== root.inboxSource) {
            root.inboxSource = source;
            root.knownStatuses = null;
        }
        root.processAlerts(snapshot);
        root.publish(snapshot);
    }

    function alertTitle(status) {
        switch (status) {
        case "limited":
            return "Usage limit reached";
        case "request":
            return "Guest request";
        case "approval":
            return "Approval needed";
        case "input":
            return "Input needed";
        case "failed":
            return "Failed needed";
        }
        return "T3 Code";
    }

    function localState(local) {
        if (!local)
            return "connecting";
        if (local.live)
            return "online";
        return local.ok ? "offline" : "unavailable";
    }

    // The inbox for the selection. Local rows keep their fields; remote rows arrive from the
    // live process tagged with their environment and links. Each row says whether its source
    // is live, and only live rows count toward the bar's status.
    function composeInbox() {
        const local = root.liveInbox || root.localInbox;
        const state = root.remoteState;
        const remotes = state && state.list ? state.list : [];
        const localId = (local && local.environmentId) || (state ? state.localId : null) || "";
        const info = {
            "registryError": state ? state.registryError : null,
            "keyring": state ? !!state.keyring : false
        };
        const localEntry = {
            "id": localId,
            "label": "Local",
            "local": true,
            "state": root.localState(local),
            "machine": null
        };
        if (!remotes.length) {
            if (!local)
                return null;
            return Object.assign({}, local, {
                "environments": [localEntry],
                "environmentInfo": info,
                "selection": "all",
                "localLive": !!local.live
            });
        }
        const sel = root.selection;
        const includeLocal = sel === "all" || sel === localId;
        // A remote-only setup has no local database; that is not an error worth showing.
        const localMissing = !local || (!local.ok && local.errorCode === "missing");
        const threads = [];
        const counted = [];
        if (includeLocal && local && local.ok) {
            for (const thread of local.threads || []) {
                const row = Object.assign({}, thread, {
                    "key": "local/" + thread.id,
                    "environmentId": localId,
                    "sourceLive": !!local.live
                });
                threads.push(row);
                if (local.live)
                    counted.push(row);
            }
        }
        const notices = [];
        let anyLive = includeLocal && !!local && !!local.live;
        for (const env of remotes) {
            if (sel !== "all" && sel !== env.id)
                continue;
            if (!env.connected)
                notices.push(env.label + ": " + env.text);
            anyLive = anyLive || env.connected;
            for (const thread of env.threads || []) {
                const row = Object.assign({}, thread, {
                    "environmentLabel": env.label,
                    "sourceLive": env.connected
                });
                threads.push(row);
                if (env.connected)
                    counted.push(row);
            }
        }
        const known = sel === "all" || sel === localId || remotes.some(env => env.id === sel);
        const base = local || {
            "port": null,
            "stateDir": ""
        };
        return Object.assign({}, base, {
            "ok": includeLocal && !localMissing ? !!local.ok : true,
            "error": includeLocal && !localMissing ? local.error : null,
            "errorCode": includeLocal && !localMissing ? local.errorCode : null,
            "serverOnline": includeLocal && !localMissing ? !!local.serverOnline : true,
            "environmentId": localId,
            "threads": threads,
            "live": anyLive,
            "localLive": includeLocal && !!local && !!local.live,
            "summary": Fmt.summarize(counted),
            "environments": [localEntry].concat(remotes.map(env => Object.assign({}, env, {
                    "threads": undefined
                }))),
            "environmentInfo": info,
            "environmentNotices": known ? notices : ["The selected environment is not available."],
            "selection": sel
        });
    }

    // The first snapshot is a baseline and never alerts, and so is the first one after a
    // remote environment (re)connects or the selection changes.
    function processAlerts(snapshot) {
        const current = {};
        for (const thread of snapshot.threads || [])
            current[thread.key || thread.id] = thread.status;
        const known = root.knownStatuses;
        root.knownStatuses = current;
        const fresh = {};
        const sessions = {};
        for (const env of snapshot.environments || []) {
            if (env.local)
                continue;
            sessions[env.id] = env.session;
            if (root.envSessions[env.id] !== env.session)
                fresh[env.id] = true;
        }
        root.envSessions = sessions;
        if (known === null || !root.notificationsEnabled)
            return;
        for (const thread of snapshot.threads || []) {
            const key = thread.key || thread.id;
            if (root.alertingStatuses.indexOf(thread.status) < 0 || known[key] === thread.status)
                continue;
            if (thread.remote ? (!thread.sourceLive || fresh[thread.environmentId]) : !snapshot.localLive)
                continue;
            const body = thread.remote ? thread.title + " · " + thread.environmentLabel : thread.title;
            Quickshell.execDetached(["notify-send", "--app-name=T3 Code", "--icon=" + decodeURIComponent(Qt.resolvedUrl("assets/T3Logo.png").toString().replace("file://", "")), root.alertTitle(thread.status), body]);
        }
    }

    Process {
        id: watcher
        command: root.command()
        running: true

        stdout: SplitParser {
            onRead: line => {
                let snapshot;
                try {
                    snapshot = JSON.parse(line);
                } catch (e) {
                    console.warn("t3codeBar: bad snapshot", e);
                    return;
                }
                snapshot.receivedAt = Date.now();
                root.restarts = 0;
                root.localInbox = snapshot;
                if (!root.liveInbox)
                    root.publishInbox();
            }
        }

        stderr: SplitParser {
            onRead: line => console.warn("t3codeBar:", line)
        }

        onExited: (exitCode, exitStatus) => {
            if (root.manualRestart) {
                root.manualRestart = false;
                restartTimer.interval = 100;
                restartTimer.restart();
                return;
            }
            root.localInbox = {
                "ok": false,
                "error": "The T3 Code reader stopped (exit " + exitCode + "). Restarting…",
                "errorCode": "backend",
                "threads": [],
                "live": false,
                "serverOnline": false,
                "summary": {
                    "status": "settled",
                    "label": "Settled",
                    "showsLabel": false,
                    "badge": 0,
                    "attention": 0,
                    "running": 0
                }
            };
            if (!root.liveInbox)
                root.publishInbox();
            restartTimer.interval = Math.min(30000, 1000 * Math.pow(2, root.restarts));
            root.restarts += 1;
            restartTimer.restart();
        }
    }

    Timer {
        id: restartTimer
        repeat: false
        onTriggered: watcher.running = true
    }

    // Settings that change the command restart the reader.
    onStateDirChanged: {
        restartWatcher();
        liveProcess.running = false;
    }
    onPollSecondsChanged: restartWatcher()

    function restartWatcher() {
        if (!watcher.running) {
            restartTimer.interval = 100;
            restartTimer.restart();
            return;
        }
        root.knownStatuses = null;
        root.manualRestart = true;
        watcher.running = false;
    }

    // The widget asks for an immediate re-read by bumping this global var.
    Connections {
        target: root.pluginService
        function onGlobalVarChanged(changedPluginId, varName) {
            if (changedPluginId !== root.pluginId)
                return;
            if (varName === "retry")
                root.restartWatcher();
            else if (varName === "liveCommand")
                root.forwardLiveCommand();
        }
    }

    // ----- live connection -----

    function liveCommandLine() {
        const cmd = ["python3", "-I", root.liveScriptPath];
        if (root.stateDir)
            cmd.push("--state-dir", root.stateDir);
        cmd.push("--clock", root.clock24 ? "24" : "12");
        return cmd;
    }

    // Hands a command to the live process and clears it at once, so a pairing
    // code does not stay in the shell's shared state.
    function forwardLiveCommand() {
        const command = pluginService ? pluginService.getGlobalVar(pluginId, "liveCommand", null) : null;
        if (!command || !command.cmd)
            return;
        pluginService.setGlobalVar(pluginId, "liveCommand", null);
        if (!liveProcess.running) {
            // A usage request still gets a reply, so the widget reads the usage cache instead.
            if (command.cmd === "usage")
                root.publishUsage({
                    "period": command.period,
                    "seq": command.seq,
                    "report": null,
                    "error": "Not connected."
                });
            else if (command.cmd === "addEnvironment" && root.pluginService)
                root.pluginService.setGlobalVar(root.pluginId, "pairingLive", {
                    "seq": command.seq,
                    "ok": false,
                    "error": "The connection process is restarting. Try again in a moment.",
                    "receivedAt": Date.now()
                });
            return;
        }
        liveProcess.write(JSON.stringify(command) + "\n");
    }

    function publishUsage(reply) {
        reply.receivedAt = Date.now();
        if (pluginService)
            pluginService.setGlobalVar(pluginId, "usageLive", reply);
    }

    function handleLiveLine(message) {
        if (message.kind === "limits") {
            if (root.pluginService)
                root.pluginService.setGlobalVar(root.pluginId, "limitsLive", message.view);
        } else if (message.kind === "inbox") {
            const snapshot = message.snapshot || null;
            if (snapshot)
                snapshot.receivedAt = Date.now();
            if (!snapshot && !root.liveInbox)
                return;
            root.liveInbox = snapshot;
            root.publishInbox();
        } else if (message.kind === "usage") {
            root.publishUsage(message);
        } else if (message.kind === "environments") {
            root.remoteState = message.state || null;
            root.publishInbox();
        } else if (message.kind === "pairing" || message.kind === "preview") {
            message.receivedAt = Date.now();
            if (root.pluginService)
                root.pluginService.setGlobalVar(root.pluginId, message.kind === "pairing" ? "pairingLive" : "previewLive", message);
        }
    }

    onClock24Changed: {
        if (liveProcess.running)
            liveProcess.write(JSON.stringify({
                "cmd": "clock",
                "twentyFour": root.clock24
            }) + "\n");
    }

    Process {
        id: liveProcess
        command: root.liveCommandLine()
        running: true
        stdinEnabled: true

        stdout: SplitParser {
            splitMarker: "\n"
            onRead: line => {
                let message;
                try {
                    message = JSON.parse(line);
                } catch (e) {
                    console.warn("t3codeBar: bad live message", e);
                    return;
                }
                root.liveRestarts = 0;
                if (!root.selectionSent) {
                    root.selectionSent = true;
                    root.sendSelection();
                }
                root.handleLiveLine(message);
            }
        }

        stderr: SplitParser {
            onRead: line => console.warn("t3codeBar live:", line)
        }

        // An unremembered token lives only in this process, so a restart needs a new pairing,
        // as when the macOS app quits.
        onExited: (exitCode, exitStatus) => {
            root.selectionSent = false;
            if (root.liveInbox || root.remoteState) {
                root.liveInbox = null;
                root.remoteState = null;
                root.publishInbox();
            }
            liveRestartTimer.interval = Math.min(30000, 2000 * Math.pow(2, root.liveRestarts));
            root.liveRestarts += 1;
            liveRestartTimer.restart();
        }
    }

    Timer {
        id: liveRestartTimer
        repeat: false
        onTriggered: liveProcess.running = true
    }
}
