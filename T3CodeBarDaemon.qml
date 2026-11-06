import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Services
import qs.Modules.Plugins

// Runs `backend/t3bar.py watch` once for every bar and screen, publishes each
// inbox snapshot as the "inbox" global var, and raises desktop notifications
// when a thread starts needing attention.
PluginComponent {
    id: root

    readonly property string scriptPath: decodeURIComponent(Qt.resolvedUrl("backend/t3bar.py").toString().replace("file://", ""))
    readonly property string stateDir: (pluginData.stateDir || "").trim()
    readonly property real pollSeconds: Math.max(0.5, Number(pluginData.pollSeconds) || 1)
    readonly property bool notificationsEnabled: pluginData.notifications !== undefined ? pluginData.notifications : true

    // Statuses that raise a notification when a thread enters them (AttentionTracker).
    readonly property var alertingStatuses: ["approval", "input", "request", "failed", "limited"]
    property var knownStatuses: null
    property int restarts: 0
    property bool manualRestart: false

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

    // The first snapshot is a baseline and never alerts.
    function processAlerts(snapshot) {
        const current = {};
        for (const thread of snapshot.threads || [])
            current[thread.id] = thread.status;
        const known = root.knownStatuses;
        root.knownStatuses = current;
        if (known === null || !snapshot.live || !root.notificationsEnabled)
            return;
        for (const thread of snapshot.threads || []) {
            if (root.alertingStatuses.indexOf(thread.status) < 0 || known[thread.id] === thread.status)
                continue;
            Quickshell.execDetached(["notify-send", "--app-name=T3 Code", "--icon=" + decodeURIComponent(Qt.resolvedUrl("assets/T3Logo.png").toString().replace("file://", "")), root.alertTitle(thread.status), thread.title]);
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
                root.processAlerts(snapshot);
                root.publish(snapshot);
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
            root.publish({
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
            });
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
    onStateDirChanged: restartWatcher()
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
            if (changedPluginId === root.pluginId && varName === "retry")
                root.restartWatcher();
        }
    }
}
