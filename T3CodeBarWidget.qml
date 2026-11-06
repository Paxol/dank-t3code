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
    // The Limits tab rendered by the live connection, and the commands sent to it.
    PluginGlobalVar {
        id: limitsLiveVar
        varName: "limitsLive"
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

    readonly property var inbox: inboxVar.value
    readonly property var limitsLive: limitsLiveVar.value

    function sendLiveCommand(command) {
        liveCommandVar.set(Object.assign({
            "seq": Date.now()
        }, command));
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

    function threadUrl(threadId, browser) {
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

    function openThread(threadId) {
        openUrl(threadUrl(threadId, false));
    }

    function openThreadInBrowser(threadId) {
        openUrl(threadUrl(threadId, true));
    }

    function copyThreadLink(threadId) {
        const url = threadUrl(threadId, false);
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
        const cached = previewCache[thread.id];
        if (cached && cached.updatedAt === thread.updatedAt)
            return cached.data;
        return null;
    }

    function loadPreview(thread) {
        if (preview(thread))
            return;
        const updatedAt = thread.updatedAt;
        backend("preview." + thread.id, ["preview", thread.id], data => {
            if (!data)
                return;
            const next = Object.assign({}, previewCache);
            next[thread.id] = {
                "updatedAt": updatedAt,
                "data": data
            };
            previewCache = next;
            previewLoaded(thread.id);
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
                            visible: root.screen === "usage"
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
                                text: root.screen === "usage" ? "Usage" : "T3 Code"
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

                        delegate: ThreadRow {
                            required property var modelData
                            width: threadList.width
                            host: root
                            thread: modelData
                            liveStatus: root.live
                            onActivated: root.openThread(modelData.id)
                            onMenuRequested: (item, x, y) => popout.openMenu(modelData, item, x, y)
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
                                    const id = popout.menuThread.id;
                                    popout.menuThread = null;
                                    if (modelData.action === "app")
                                        root.openThread(id);
                                    else if (modelData.action === "browser")
                                        root.openThreadInBrowser(id);
                                    else
                                        root.copyThreadLink(id);
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
