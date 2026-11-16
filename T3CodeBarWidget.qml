import QtQuick
import Quickshell
import qs.Common
import qs.Services
import qs.Widgets
import qs.DCommon.Widgets
import qs.Modules.Plugins
import "T3Fmt.js" as Fmt

// The T3 Code inbox in the bar: the T3 mark with the strongest thread status,
// and a popout with the thread list and the Usage screen.
PluginComponent {
    id: root

    layerNamespacePlugin: "t3code-bar"
    popoutWidth: 400

    readonly property string scriptPath: decodeURIComponent(Qt.resolvedUrl("backend/t3bar.py").toString().replace("file://", ""))
    readonly property string logoPath: Qt.resolvedUrl("assets/T3Logo.png")

    // Settings
    readonly property string stateDir: (pluginData.stateDir || "").trim()
    readonly property bool showPreviews: pluginData.showPreviews !== undefined ? pluginData.showPreviews : true
    readonly property bool t3Colors: pluginData.t3Colors !== undefined ? pluginData.t3Colors : true
    readonly property bool clock24: pluginData.clock24 !== undefined ? pluginData.clock24 : true
    readonly property bool showBarLabel: pluginData.showBarLabel !== undefined ? pluginData.showBarLabel : true
    readonly property bool hideWhenEmpty: pluginData.hideWhenEmpty !== undefined ? pluginData.hideWhenEmpty : false
    readonly property real maxListHeight: Math.max(240, Number(pluginData.maxListHeight) || 520)
    readonly property string desktopId: (pluginData.desktopId || "com.t3tools.T3Code").trim()

    // Shared inbox, written by T3CodeBarDaemon.
    PluginGlobalVar {
        id: inboxVar
        varName: "inbox"
        defaultValue: null
    }
    // The Limits tab rendered by the live connection, its replies to usage
    // requests, and the commands sent to it.
    PluginGlobalVar {
        id: limitsLiveVar
        varName: "limitsLive"
        defaultValue: null
    }
    PluginGlobalVar {
        id: usageLiveVar
        varName: "usageLive"
        defaultValue: null
    }
    PluginGlobalVar {
        id: liveCommandVar
        varName: "liveCommand"
        defaultValue: null
    }
    PluginGlobalVar {
        id: retryVar
        varName: "retry"
        defaultValue: 0
    }
    // Replies from the live process: remote previews, and the result of a pairing link.
    PluginGlobalVar {
        id: previewLiveVar
        varName: "previewLive"
        defaultValue: null
        onValueChanged: root.acceptRemotePreview(value)
    }
    PluginGlobalVar {
        id: pairingLiveVar
        varName: "pairingLive"
        defaultValue: null
    }

    readonly property var inbox: inboxVar.value
    readonly property var limitsLive: limitsLiveVar.value
    readonly property var usageLive: usageLiveVar.value
    // Cost and tokens come from the server when the pairing includes View diagnostics and usage.
    readonly property bool liveUsage: !!limitsLive && !!limitsLive.live && !!limitsLive.live.usage
    readonly property var pairingLive: pairingLiveVar.value

    // Environments: Local and every paired remote one. The selection is "all" or one ID.
    readonly property var environments: inbox && inbox.environments ? inbox.environments : []
    readonly property bool multiEnvironment: environments.length > 1
    readonly property string selection: pluginData.environment || "all"
    readonly property string localEnvironmentId: inbox && inbox.environmentId ? inbox.environmentId : ""
    // Usage comes from the servers when the selection includes a remote environment.
    readonly property bool usageFromServer: liveUsage || (multiEnvironment && selection !== localEnvironmentId)

    function environmentById(id) {
        return environments.find(env => env.id === id) || null;
    }

    function selectionLabel() {
        if (selection === "all")
            return "All environments";
        const env = environmentById(selection);
        return env ? env.label : "Unavailable environment";
    }

    function selectEnvironment(id) {
        saveSetting("environment", id);
    }

    function sendLiveCommand(command) {
        liveCommandVar.set(Object.assign({
            "seq": Date.now()
        }, command));
    }

    property int usageSeq: 0
    // Returns the request's seq; the reply in usageLive carries it back.
    function requestLiveUsage(period) {
        usageSeq += 1;
        const seq = Date.now() * 1000 + usageSeq % 1000;
        sendLiveCommand({
            "cmd": "usage",
            "period": period,
            "seq": seq,
            "environment": selection
        });
        return seq;
    }
    readonly property bool loading: !inbox
    readonly property var threads: inbox ? (inbox.threads || []) : []
    readonly property bool live: inbox ? !!inbox.live : false
    readonly property var summary: inbox && inbox.summary ? inbox.summary : ({
            "status": "settled",
            "label": "Settled",
            "showsLabel": false,
            "badge": 0,
            "attention": 0,
            "running": 0
        })

    visibilityCommand: ""
    Component.onCompleted: updateVisibility()
    onThreadsChanged: updateVisibility()
    onHideWhenEmptyChanged: updateVisibility()
    function updateVisibility() {
        if (hideWhenEmpty && !loading && threads.length === 0)
            setVisibilityOverride(false);
        else
            clearVisibilityOverride();
    }

    // Navigation. The popout always opens on the threads.
    property string screen: "threads"
    property string usageTab: pluginData.usageTab || "limits"
    property int usagePeriod: Number(pluginData.usagePeriod) || 30
    property real now: Date.now()

    onInteractionActiveChanged: {
        now = Date.now();
        if (!interactionActive)
            screen = "threads";
    }

    Timer {
        interval: 1000
        repeat: true
        running: root.interactionActive
        onTriggered: root.now = Date.now()
    }

    function saveSetting(key, value) {
        if (pluginService)
            pluginService.savePluginData(pluginId, key, value);
    }

    function setUsageTab(tab) {
        usageTab = tab;
        saveSetting("usageTab", tab);
    }

    function setUsagePeriod(days) {
        usagePeriod = days;
        saveSetting("usagePeriod", days);
    }

    // ----- status presentation -----

    function statusColor(status) {
        if (t3Colors) {
            switch (status) {
            case "working":
            case "goal":
                return "#60a5fa";
            case "approval":
            case "woke":
            case "limited":
                return "#fbbf24";
            case "input":
                return "#a5b4fc";
            case "request":
                return "#f0abfc";
            case "failed":
                return "#f87171";
            case "done":
                return "#34d399";
            case "waiting":
                return "#a3a3a3";
            }
            return Theme.surfaceVariantText;
        }
        switch (status) {
        case "working":
        case "goal":
            return Theme.info;
        case "approval":
        case "woke":
        case "limited":
            return Theme.warning;
        case "input":
            return Theme.primary;
        case "request":
            return Theme.tertiary;
        case "failed":
            return Theme.error;
        case "done":
            return Theme.success;
        }
        return Theme.surfaceVariantText;
    }

    function statusIcon(status) {
        switch (status) {
        case "working":
        case "goal":
            return "progress_activity";
        case "waiting":
            return "pending";
        case "approval":
            return "gpp_maybe";
        case "input":
            return "contact_support";
        case "request":
            return "person";
        case "failed":
        case "limited":
            return "error";
        case "woke":
            return "alarm";
        case "done":
            return "check_circle";
        }
        return "";
    }

    function statusLabel(status) {
        const labels = {
            "working": "Working",
            "goal": "Goal",
            "approval": "Approval",
            "input": "Input",
            "request": "Request",
            "woke": "Woke",
            "failed": "Failed",
            "limited": "Limited",
            "waiting": "Waiting",
            "done": "Done",
            "settled": "Settled"
        };
        return labels[status] || status;
    }

    function pillSuffix(thread) {
        switch (thread.status) {
        case "working":
        case "goal":
        case "waiting":
            return thread.statusSince ? Fmt.elapsed(thread.statusSince, now) : "";
        case "limited":
            const left = Fmt.until(thread.limitResetAt, now);
            return left ? "· " + left : "";
        }
        return "";
    }

    function summaryText() {
        if (loading)
            return "Reading…";
        if (!live)
            return inbox && inbox.ok ? "Offline" : "Unavailable";
        const parts = [];
        if (summary.running > 0)
            parts.push(summary.running + " working");
        const waiting = summary.attention - summary.running;
        if (waiting > 0)
            parts.push(waiting + " need" + (waiting === 1 ? "s" : "") + " you");
        return parts.length ? parts.join(" · ") : threads.length + (threads.length === 1 ? " thread" : " threads");
    }

    // ----- actions -----

    // Remote rows carry their own links; local ones are built here.
    function threadUrl(thread, browser) {
        if (thread.appUrl) {
            if (browser && !thread.browserUrl) {
                ToastService.showError("T3 Code", "This environment has no browser address.");
                return "";
            }
            return browser ? thread.browserUrl : thread.appUrl;
        }
        const threadId = thread.id;
        const env = inbox ? inbox.environmentId : null;
        if (!env) {
            ToastService.showError("T3 Code", "Environment ID not found. Open T3 Code and check the data directory.");
            return "";
        }
        const path = "/" + encodeURIComponent(env) + "/" + encodeURIComponent(threadId);
        if (browser) {
            if (!inbox.port) {
                ToastService.showError("T3 Code", "Local server address not found. Open T3 Code before opening a browser link.");
                return "";
            }
            // Always loopback, even when the server binds 0.0.0.0 or advertises a LAN origin.
            return "http://127.0.0.1:" + inbox.port + path;
        }
        return "t3code://threads" + path;
    }

    function openUrl(url) {
        if (!url)
            return;
        Quickshell.execDetached(["xdg-open", url]);
        closePopout();
    }

    function openThread(thread) {
        openUrl(threadUrl(thread, false));
    }

    function openThreadInBrowser(thread) {
        openUrl(threadUrl(thread, true));
    }

    function copyThreadLink(thread) {
        const url = threadUrl(thread, false);
        if (!url)
            return;
        Quickshell.execDetached(["dms", "cl", "copy", url]);
        ToastService.showInfo("T3 Code", "Thread link copied");
    }

    function openNewThread() {
        openUrl("t3code://new");
    }

    function activateT3Code() {
        const ids = [desktopId, desktopId + ".desktop", "t3code", "com.t3tools.T3Code"];
        for (const id of ids) {
            const entry = DesktopEntries.byId(id);
            if (entry) {
                entry.execute();
                closePopout();
                return;
            }
        }
        const guess = DesktopEntries.heuristicLookup("t3code");
        if (guess) {
            guess.execute();
            closePopout();
            return;
        }
        ToastService.showError("T3 Code", "T3 Code could not be found. Set its desktop entry ID in the plugin settings.");
    }

    function retry() {
        retryVar.set((retryVar.value || 0) + 1);
    }

    // ----- backend calls -----

    function backend(name, args, callback) {
        const cmd = ["python3", "-I", scriptPath];
        if (stateDir)
            cmd.push("--state-dir", stateDir);
        Proc.runCommand("t3codeBar." + name, cmd.concat(args), (stdout, exitCode) => {
            if (exitCode !== 0) {
                callback(null);
                return;
            }
            try {
                callback(JSON.parse(stdout));
            } catch (e) {
                callback(null);
            }
        }, 0);
    }

    // Hover previews: the start of a thread's last assistant reply, cached per update.
    property var previewCache: ({})
    signal previewLoaded(string threadId)

    function preview(thread) {
        const cached = previewCache[thread.key || thread.id];
        if (cached && cached.updatedAt === thread.updatedAt)
            return cached.data;
        return null;
    }

    function storePreview(key, updatedAt, data) {
        const next = Object.assign({}, previewCache);
        next[key] = {
            "updatedAt": updatedAt,
            "data": data
        };
        previewCache = next;
        previewLoaded(key);
    }

    // Remote previews in flight, by request seq.
    property var pendingPreviews: ({})

    function loadPreview(thread) {
        if (preview(thread))
            return;
        const key = thread.key || thread.id;
        const updatedAt = thread.updatedAt;
        if (thread.remote) {
            const seq = Date.now() * 1000 + Math.floor(Math.random() * 1000);
            const pending = Object.assign({}, pendingPreviews);
            pending[seq] = {
                "key": key,
                "updatedAt": updatedAt
            };
            pendingPreviews = pending;
            sendLiveCommand({
                "cmd": "preview",
                "environment": thread.environmentId,
                "thread": thread.id,
                "seq": seq
            });
            return;
        }
        backend("preview." + thread.id, ["preview", thread.id], data => {
            if (data)
                storePreview(key, updatedAt, data);
        });
    }

    function acceptRemotePreview(reply) {
        if (!reply || !pendingPreviews[reply.seq])
            return;
        const request = pendingPreviews[reply.seq];
        const pending = Object.assign({}, pendingPreviews);
        delete pending[reply.seq];
        pendingPreviews = pending;
        if (reply.ok)
            storePreview(request.key, request.updatedAt, {
                "text": reply.text || "",
                "updatedAt": reply.updatedAt
            });
    }

    // ----- bar pill -----

    pillRightClickAction: () => root.activateT3Code()

    component BarMark: Item {
        id: mark
        property real size: 16
        property string status: "settled"
        property color dotColor: "transparent"
        property bool dimmed: false
        implicitWidth: size
        implicitHeight: size

        Image {
            anchors.fill: parent
            source: root.logoPath
            sourceSize: Qt.size(mark.size * 2, mark.size * 2)
            smooth: true
            mipmap: true
            opacity: mark.dimmed ? 0.45 : 1
        }

        Rectangle {
            visible: mark.status !== "settled"
            width: Math.max(5, Math.round(mark.size * 0.42))
            height: width
            radius: width / 2
            color: mark.dotColor
            border.width: 1.5
            border.color: Theme.withAlpha(Theme.surface, 0.9)
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.rightMargin: -2
            anchors.bottomMargin: -2
        }
    }

    horizontalBarPill: Component {
        Row {
            spacing: Theme.spacingXS

            BarMark {
                size: root.iconSize
                status: root.summary.status
                dotColor: root.statusColor(root.summary.status)
                dimmed: !root.live
                anchors.verticalCenter: parent.verticalCenter
            }

            Rectangle {
                visible: root.showBarLabel && root.summary.showsLabel
                anchors.verticalCenter: parent.verticalCenter
                height: labelText.implicitHeight + 2
                width: labelText.implicitWidth + Theme.spacingS * 2
                radius: height / 2
                color: root.statusColor(root.summary.status)

                StyledText {
                    id: labelText
                    anchors.centerIn: parent
                    text: root.summary.badge > 1 ? root.summary.label + " " + root.summary.badge : root.summary.label
                    font.pixelSize: Math.max(10, root.textSize - 1)
                    font.weight: Font.Medium
                    color: "#000000"
                    opacity: 0.85
                }
            }

            StyledText {
                visible: root.showBarLabel && !root.summary.showsLabel && root.summary.badge > 1
                anchors.verticalCenter: parent.verticalCenter
                text: root.summary.badge
                font.pixelSize: root.textSize
                font.weight: Font.Medium
                color: Theme.widgetTextColor
            }
        }
    }

    verticalBarPill: Component {
        Column {
            spacing: 2

            BarMark {
                size: root.iconSize
                status: root.summary.status
                dotColor: root.statusColor(root.summary.status)
                dimmed: !root.live
                anchors.horizontalCenter: parent.horizontalCenter
            }

            StyledText {
                visible: root.showBarLabel && root.summary.badge > 0 && root.summary.status !== "settled"
                anchors.horizontalCenter: parent.horizontalCenter
                text: root.summary.badge
                font.pixelSize: Math.max(10, root.textSize - 1)
                font.weight: Font.Medium
                color: root.summary.showsLabel ? root.statusColor(root.summary.status) : Theme.widgetTextColor
            }
        }
    }

    // ----- popout -----

    popoutContent: Component {
        Item {
            id: popout
            implicitHeight: content.implicitHeight
            property var closePopout: null

            // Context menu of a thread row.
            property var menuThread: null
            property point menuAt: Qt.point(0, 0)

            function openMenu(thread, item, x, y) {
                menuAt = item.mapToItem(popout, x, y);
                menuThread = thread;
            }

            // The environment picker's menu.
            property bool environmentMenuOpen: false
            property point environmentMenuAt: Qt.point(0, 0)

            function openEnvironmentMenu(item) {
                environmentMenuAt = item.mapToItem(popout, 0, item.height + 4);
                environmentMenuOpen = true;
            }

            // Hover preview of a thread row, floated over the rows around it.
            property var previewThread: null
            property rect previewRowRect: Qt.rect(0, 0, 0, 0)
            readonly property var previewData: previewThread && root.showPreviews ? root.preview(previewThread) : null
            readonly property bool previewShown: !!previewData && previewData.text.length > 0 && menuThread === null && !environmentMenuOpen
            // The last preview shown, kept so the card can fade out with its text.
            property var shownPreview: null
            onPreviewDataChanged: if (previewData)
                shownPreview = previewData

            function showPreview(thread, item) {
                const at = item.mapToItem(popout, 0, 0);
                previewRowRect = Qt.rect(at.x, at.y, item.width, item.height);
                previewThread = thread;
            }

            function hidePreview(thread) {
                if (previewThread && (previewThread.key || previewThread.id) === (thread.key || thread.id))
                    previewThread = null;
            }

            Column {
                id: content
                width: parent.width
                spacing: Theme.spacingS

                // Header
                Item {
                    width: parent.width
                    height: 36

                    Row {
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: Theme.spacingS

                        HeaderButton {
                            visible: root.screen !== "threads"
                            iconName: "arrow_back"
                            tip: "Back to threads"
                            onClicked: root.screen = "threads"
                            anchors.verticalCenter: parent.verticalCenter
                        }

                        BarMark {
                            visible: root.screen === "threads"
                            size: 22
                            status: root.summary.status
                            dotColor: root.statusColor(root.summary.status)
                            dimmed: !root.live
                            anchors.verticalCenter: parent.verticalCenter
                        }

                        Column {
                            anchors.verticalCenter: parent.verticalCenter

                            StyledText {
                                text: root.screen === "usage" ? "Usage" : (root.screen === "environments" ? "Environments" : "T3 Code")
                                font.pixelSize: Theme.fontSizeLarge
                                font.weight: Font.Medium
                                color: Theme.surfaceText
                            }

                            StyledText {
                                visible: root.screen === "threads"
                                text: root.summaryText()
                                font.pixelSize: Theme.fontSizeSmall
                                color: Theme.surfaceVariantText
                            }

                        }
                    }

                    Row {
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: Theme.spacingXS

                        HeaderButton {
                            iconName: "lan"
                            tip: "Environments"
                            onClicked: root.screen = "environments"
                        }

                        HeaderButton {
                            iconName: "edit_square"
                            tip: "New thread"
                            onClicked: root.openNewThread()
                        }

                        HeaderButton {
                            iconName: "open_in_new"
                            tip: "Open T3 Code"
                            onClicked: root.activateT3Code()
                        }
                    }
                }

                // The environment picker, once there is more than one environment.
                Rectangle {
                    visible: root.multiEnvironment && root.screen !== "environments"
                    width: chipRow.implicitWidth + Theme.spacingS * 2
                    height: 22
                    radius: 11
                    color: chipArea.containsMouse ? Theme.withAlpha(Theme.surfaceText, 0.12) : Theme.withAlpha(Theme.surfaceText, 0.06)

                    Row {
                        id: chipRow
                        anchors.centerIn: parent
                        spacing: 4

                        DIcon {
                            name: root.selection === "all" ? "lan" : (root.selection === root.localEnvironmentId ? "computer" : Fmt.machineIcon((root.environmentById(root.selection) || {}).machine))
                            size: 14
                            color: Theme.surfaceVariantText
                            anchors.verticalCenter: parent.verticalCenter
                        }

                        StyledText {
                            text: root.selectionLabel()
                            font.pixelSize: Theme.fontSizeSmall - 1
                            color: Theme.surfaceText
                            anchors.verticalCenter: parent.verticalCenter
                        }

                        DIcon {
                            name: "expand_more"
                            size: 14
                            color: Theme.surfaceVariantText
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }

                    MouseArea {
                        id: chipArea
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: popout.openEnvironmentMenu(parent)
                    }

                    Accessible.name: "Environment: " + root.selectionLabel()
                    Accessible.role: Accessible.Button
                }

                // Threads screen
                Column {
                    width: parent.width
                    spacing: Theme.spacingXS
                    visible: root.screen === "threads"

                    Notice {
                        visible: root.loading
                        width: parent.width
                        iconName: "hourglass_empty"
                        text: "Reading T3 Code data…"
                    }

                    Notice {
                        visible: !root.loading && !!root.inbox && !root.inbox.ok
                        width: parent.width
                        iconName: "warning"
                        text: root.inbox && root.inbox.error ? root.inbox.error : ""
                        showActions: true
                    }

                    Notice {
                        visible: !root.loading && !!root.inbox && root.inbox.ok && !root.inbox.serverOnline
                        width: parent.width
                        iconName: "cloud_off"
                        text: "T3 Code offline · showing saved threads"
                    }

                    Repeater {
                        model: root.inbox && root.inbox.environmentNotices ? root.inbox.environmentNotices : []

                        Notice {
                            required property var modelData
                            width: parent.width
                            iconName: "cloud_off"
                            text: modelData
                        }
                    }

                    // Empty state
                    Column {
                        visible: !root.loading && root.threads.length === 0 && !!root.inbox && root.inbox.ok
                        width: parent.width
                        spacing: Theme.spacingS
                        topPadding: Theme.spacingL
                        bottomPadding: Theme.spacingL

                        DIcon {
                            name: "inbox"
                            size: 24
                            color: Theme.surfaceVariantText
                            opacity: 0.6
                            anchors.horizontalCenter: parent.horizontalCenter
                        }

                        StyledText {
                            text: "No active threads"
                            font.pixelSize: Theme.fontSizeSmall
                            color: Theme.surfaceVariantText
                            anchors.horizontalCenter: parent.horizontalCenter
                        }

                        LinkButton {
                            text: "Open T3 Code"
                            anchors.horizontalCenter: parent.horizontalCenter
                            onClicked: root.activateT3Code()
                        }
                    }

                    ListView {
                        id: threadList
                        width: parent.width
                        height: Math.min(contentHeight, root.maxListHeight)
                        visible: root.threads.length > 0
                        clip: true
                        spacing: 2
                        boundsBehavior: Flickable.StopAtBounds
                        interactive: contentHeight > height
                        model: root.threads
                        onContentYChanged: popout.previewThread = null

                        delegate: ThreadRow {
                            required property var modelData
                            width: threadList.width
                            host: root
                            thread: modelData
                            liveStatus: modelData.sourceLive !== undefined ? modelData.sourceLive : root.live
                            onActivated: root.openThread(modelData)
                            onMenuRequested: (item, x, y) => popout.openMenu(modelData, item, x, y)
                            onPreviewRequested: item => popout.showPreview(modelData, item)
                            onPreviewDismissed: popout.hidePreview(modelData)
                        }
                    }

                    // Footer: opens the Usage screen.
                    Rectangle {
                        width: parent.width
                        height: 36
                        radius: Theme.cornerRadius
                        color: usageArea.containsMouse ? Theme.withAlpha(Theme.surfaceText, 0.08) : "transparent"

                        Rectangle {
                            width: parent.width
                            height: 1
                            color: Theme.withAlpha(Theme.outline, 0.25)
                            anchors.top: parent.top
                        }

                        Row {
                            anchors.left: parent.left
                            anchors.leftMargin: Theme.spacingM
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: Theme.spacingS

                            DIcon {
                                name: "bar_chart"
                                size: 16
                                color: usageArea.containsMouse ? Theme.surfaceText : Theme.surfaceVariantText
                                anchors.verticalCenter: parent.verticalCenter
                            }

                            StyledText {
                                text: "Usage"
                                font.pixelSize: Theme.fontSizeSmall + 1
                                color: usageArea.containsMouse ? Theme.surfaceText : Theme.surfaceVariantText
                                anchors.verticalCenter: parent.verticalCenter
                            }
                        }

                        DIcon {
                            name: "chevron_right"
                            size: 16
                            color: Theme.surfaceVariantText
                            anchors.right: parent.right
                            anchors.rightMargin: Theme.spacingM
                            anchors.verticalCenter: parent.verticalCenter
                        }

                        MouseArea {
                            id: usageArea
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: root.screen = "usage"
                        }
                    }
                }

                // Usage screen
                Loader {
                    width: parent.width
                    active: root.screen === "usage"
                    visible: active
                    sourceComponent: UsageScreen {
                        host: root
                    }
                }

                // Environments screen
                Loader {
                    width: parent.width
                    active: root.screen === "environments"
                    visible: active
                    sourceComponent: EnvironmentsScreen {
                        host: root
                    }
                }
            }

            MouseArea {
                anchors.fill: parent
                visible: popout.environmentMenuOpen
                acceptedButtons: Qt.LeftButton | Qt.RightButton
                onClicked: popout.environmentMenuOpen = false
            }

            Rectangle {
                visible: popout.environmentMenuOpen
                width: 260
                height: environmentColumn.implicitHeight + Theme.spacingXS * 2
                x: Math.min(popout.environmentMenuAt.x, popout.width - width)
                y: popout.environmentMenuAt.y
                z: 2
                radius: Theme.cornerRadius
                color: Theme.surfaceContainerHighest
                border.width: 1
                border.color: Theme.withAlpha(Theme.outline, 0.3)

                Column {
                    id: environmentColumn
                    x: Theme.spacingXS
                    y: Theme.spacingXS
                    width: parent.width - Theme.spacingXS * 2

                    Repeater {
                        model: [
                            {
                                "id": "all",
                                "label": "All environments",
                                "icon": "lan",
                                "state": "online"
                            }
                        ].concat(root.environments.map(env => ({
                                    "id": env.id,
                                    "label": env.label,
                                    "icon": env.local ? "computer" : Fmt.machineIcon(env.machine),
                                    "state": env.state
                                }))).concat([
                            {
                                "id": "",
                                "label": "Manage environments…",
                                "icon": "settings",
                                "state": "online"
                            }
                        ])

                        delegate: Rectangle {
                            required property var modelData
                            width: environmentColumn.width
                            height: 32
                            radius: Theme.cornerRadius - 2
                            color: environmentItemArea.containsMouse ? Theme.withAlpha(Theme.surfaceText, 0.08) : "transparent"

                            Row {
                                anchors.left: parent.left
                                anchors.leftMargin: Theme.spacingS
                                anchors.right: checkIcon.left
                                anchors.verticalCenter: parent.verticalCenter
                                spacing: Theme.spacingS

                                DIcon {
                                    name: modelData.icon
                                    size: 16
                                    color: Theme.surfaceVariantText
                                    anchors.verticalCenter: parent.verticalCenter
                                }

                                StyledText {
                                    width: Math.min(implicitWidth, 170)
                                    text: modelData.label
                                    elide: Text.ElideRight
                                    font.pixelSize: Theme.fontSizeSmall + 1
                                    color: Theme.surfaceText
                                    anchors.verticalCenter: parent.verticalCenter
                                }

                                Rectangle {
                                    visible: modelData.id !== "" && modelData.id !== "all"
                                    width: 7
                                    height: 7
                                    radius: 4
                                    color: modelData.state === "online" ? Theme.success : (modelData.state === "offline" || modelData.state === "connecting" ? Theme.warning : Theme.surfaceVariantText)
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            DIcon {
                                id: checkIcon
                                visible: modelData.id !== "" && modelData.id === root.selection
                                name: "check"
                                size: 16
                                color: Theme.primary
                                anchors.right: parent.right
                                anchors.rightMargin: Theme.spacingS
                                anchors.verticalCenter: parent.verticalCenter
                            }

                            MouseArea {
                                id: environmentItemArea
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    popout.environmentMenuOpen = false;
                                    if (modelData.id === "")
                                        root.screen = "environments";
                                    else
                                        root.selectEnvironment(modelData.id);
                                }
                            }

                            Accessible.name: modelData.label
                            Accessible.role: Accessible.MenuItem
                        }
                    }
                }
            }

            // Hover preview card: below the row, or above it when there is no room.
            Rectangle {
                id: previewCard
                readonly property real gap: 4
                readonly property real below: popout.previewRowRect.y + popout.previewRowRect.height + gap
                readonly property real above: popout.previewRowRect.y - gap - height

                x: popout.previewRowRect.x + Theme.spacingS
                y: below + height <= popout.height || above < 0 ? Math.max(0, Math.min(below, popout.height - height)) : above
                z: 1
                width: popout.previewRowRect.width - Theme.spacingS * 2
                height: previewColumn.implicitHeight + Theme.spacingS * 2
                radius: Theme.cornerRadius
                color: Theme.surfaceContainerHighest
                border.width: 1
                border.color: Theme.withAlpha(Theme.outline, 0.3)
                opacity: popout.previewShown ? 1 : 0
                visible: opacity > 0

                Behavior on opacity {
                    NumberAnimation {
                        duration: 120
                        easing.type: Easing.OutCubic
                    }
                }

                Rectangle {
                    x: Theme.spacingS
                    y: Theme.spacingS
                    width: 2
                    height: parent.height - Theme.spacingS * 2
                    radius: 1
                    color: Theme.withAlpha(Theme.surfaceText, 0.2)
                }

                Column {
                    id: previewColumn
                    x: Theme.spacingS + 10
                    y: Theme.spacingS
                    width: parent.width - Theme.spacingS * 2 - 10
                    spacing: 3

                    StyledText {
                        width: parent.width
                        text: popout.shownPreview ? popout.shownPreview.text : ""
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.surfaceText
                        wrapMode: Text.Wrap
                        maximumLineCount: 10
                        elide: Text.ElideRight
                        textFormat: Text.PlainText
                    }

                    StyledText {
                        visible: !!popout.shownPreview && !!popout.shownPreview.updatedAt
                        text: popout.shownPreview && popout.shownPreview.updatedAt ? Fmt.ago(popout.shownPreview.updatedAt, root.now) : ""
                        font.pixelSize: Theme.fontSizeSmall - 2
                        color: Theme.surfaceVariantText
                        opacity: 0.7
                    }
                }
            }

            // Thread context menu
            MouseArea {
                anchors.fill: parent
                visible: popout.menuThread !== null
                acceptedButtons: Qt.LeftButton | Qt.RightButton
                onClicked: popout.menuThread = null
            }

            Rectangle {
                id: menu
                visible: popout.menuThread !== null
                width: 200
                height: menuColumn.implicitHeight + Theme.spacingXS * 2
                x: Math.min(popout.menuAt.x, popout.width - width)
                y: Math.min(popout.menuAt.y, popout.height - height)
                radius: Theme.cornerRadius
                color: Theme.surfaceContainerHighest
                border.width: 1
                border.color: Theme.withAlpha(Theme.outline, 0.3)

                Column {
                    id: menuColumn
                    x: Theme.spacingXS
                    y: Theme.spacingXS
                    width: parent.width - Theme.spacingXS * 2

                    Repeater {
                        model: [
                            {
                                "label": "Open in T3 Code",
                                "icon": "open_in_new",
                                "action": "app"
                            },
                            {
                                "label": "Open in Browser",
                                "icon": "public",
                                "action": "browser"
                            },
                            {
                                "label": "Copy Link",
                                "icon": "link",
                                "action": "copy"
                            }
                        ]

                        delegate: Rectangle {
                            required property var modelData
                            width: menuColumn.width
                            height: 32
                            radius: Theme.cornerRadius - 2
                            color: itemArea.containsMouse ? Theme.withAlpha(Theme.surfaceText, 0.08) : "transparent"

                            Row {
                                anchors.left: parent.left
                                anchors.leftMargin: Theme.spacingS
                                anchors.verticalCenter: parent.verticalCenter
                                spacing: Theme.spacingS

                                DIcon {
                                    name: modelData.icon
                                    size: 16
                                    color: Theme.surfaceVariantText
                                    anchors.verticalCenter: parent.verticalCenter
                                }

                                StyledText {
                                    text: modelData.label
                                    font.pixelSize: Theme.fontSizeSmall + 1
                                    color: Theme.surfaceText
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            MouseArea {
                                id: itemArea
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    const thread = popout.menuThread;
                                    popout.menuThread = null;
                                    if (modelData.action === "app")
                                        root.openThread(thread);
                                    else if (modelData.action === "browser")
                                        root.openThreadInBrowser(thread);
                                    else
                                        root.copyThreadLink(thread);
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    component HeaderButton: Rectangle {
        id: button
        property string iconName
        property string tip
        signal clicked
        width: 30
        height: 30
        radius: 15
        color: buttonArea.containsMouse ? Theme.withAlpha(Theme.surfaceText, 0.1) : "transparent"

        DIcon {
            anchors.centerIn: parent
            name: button.iconName
            size: 18
            color: Theme.surfaceText
        }

        MouseArea {
            id: buttonArea
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: button.clicked()
        }

        Accessible.name: tip
        Accessible.role: Accessible.Button
    }

    component LinkButton: Rectangle {
        id: link
        property alias text: linkLabel.text
        signal clicked
        width: linkLabel.implicitWidth + Theme.spacingM * 2
        height: 28
        radius: 14
        color: linkArea.containsMouse ? Theme.withAlpha(Theme.primary, 0.18) : Theme.withAlpha(Theme.primary, 0.1)

        StyledText {
            id: linkLabel
            anchors.centerIn: parent
            font.pixelSize: Theme.fontSizeSmall
            font.weight: Font.Medium
            color: Theme.primary
        }

        MouseArea {
            id: linkArea
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: link.clicked()
        }
    }

    component Notice: Rectangle {
        id: notice
        property string iconName
        property alias text: noticeText.text
        property bool showActions: false
        height: noticeColumn.implicitHeight + Theme.spacingS * 2
        radius: Theme.cornerRadius
        color: Theme.withAlpha(Theme.surfaceText, 0.05)

        Column {
            id: noticeColumn
            x: Theme.spacingS
            y: Theme.spacingS
            width: parent.width - Theme.spacingS * 2
            spacing: Theme.spacingS

            Row {
                width: parent.width
                spacing: Theme.spacingS

                DIcon {
                    name: notice.iconName
                    size: 16
                    color: Theme.surfaceVariantText
                }

                StyledText {
                    id: noticeText
                    width: parent.width - 16 - Theme.spacingS
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.surfaceVariantText
                    wrapMode: Text.WordWrap
                }
            }

            Row {
                visible: notice.showActions
                spacing: Theme.spacingS

                LinkButton {
                    text: "Open T3 Code"
                    onClicked: root.activateT3Code()
                }

                LinkButton {
                    text: "Retry"
                    onClicked: root.retry()
                }
            }
        }
    }
}
