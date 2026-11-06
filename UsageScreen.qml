import QtQuick
import qs.Common
import qs.DCommon.Widgets
import "T3Fmt.js" as Fmt

// The Cost, Tokens and Limits tabs of T3 Code's Usage page.
Column {
    id: usage

    required property var host

    property var report: null
    property var limits: null
    property bool loadingReport: false
    property bool loadingLimits: false
    property bool showAllModels: false
    property int hoverColumn: -1

    readonly property string tab: host.usageTab
    readonly property bool isCost: tab === "cost"
    readonly property bool reportReady: !!report && report.state === "ok"

    spacing: Theme.spacingM

    function refreshReport() {
        loadingReport = true;
        const period = host.usagePeriod;
        host.backend("usage", ["usage", "--period", String(period)], data => {
            loadingReport = false;
            // A failed read keeps the last numbers, as T3 Code Bar does while the cache is rewritten.
            if (data && (data.state !== "busy" || !report) && period === host.usagePeriod)
                report = data;
        });
    }

    function refreshLimits() {
        loadingLimits = true;
        host.backend("limits", ["limits", "--clock", host.clock24 ? "24" : "12"], data => {
            loadingLimits = false;
            if (data)
                limits = data;
        });
    }

    function refresh() {
        if (tab === "limits")
            refreshLimits();
        else
            refreshReport();
    }

    Component.onCompleted: refresh()
    onTabChanged: refresh()

    Connections {
        target: usage.host
        function onUsagePeriodChanged() {
            usage.report = null;
            usage.showAllModels = false;
            usage.refreshReport();
        }
    }

    // Limits every 5 seconds, transcript usage every 30, while the screen is open.
    Timer {
        interval: usage.tab === "limits" ? 5000 : 30000
        repeat: true
        running: usage.host.interactionActive
        onTriggered: usage.refresh()
    }

    // ----- tabs and period -----

    Item {
        width: parent.width
        height: 32

        Segmented {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            options: [
                {
                    "value": "cost",
                    "label": "Cost"
                },
                {
                    "value": "tokens",
                    "label": "Tokens"
                },
                {
                    "value": "limits",
                    "label": "Limits"
                }
            ]
            current: usage.tab
            onPicked: value => usage.host.setUsageTab(value)
        }

        Segmented {
            visible: usage.tab !== "limits"
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            compact: true
            options: [
                {
                    "value": 1,
                    "label": "24h"
                },
                {
                    "value": 7,
                    "label": "7d"
                },
                {
                    "value": 30,
                    "label": "30d"
                },
                {
                    "value": 90,
                    "label": "90d"
                }
            ]
            current: usage.host.usagePeriod
            onPicked: value => usage.host.setUsagePeriod(value)
        }
    }

    Flickable {
        id: scroller
        width: parent.width
        height: Math.min(contentHeight, usage.host.maxListHeight)
        contentHeight: body.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
            id: body
            width: scroller.width
            spacing: Theme.spacingM

            // ===== Cost and Tokens =====

            Message {
                visible: usage.tab !== "limits" && !usage.report
                text: "Reading usage…"
            }

            Message {
                visible: usage.tab !== "limits" && !!usage.report && usage.report.state !== "ok"
                text: usage.report ? (usage.report.message || "") : ""
            }

            Column {
                visible: usage.tab !== "limits" && usage.reportReady
                width: parent.width
                spacing: Theme.spacingM

                // Headline
                Column {
                    width: parent.width
                    spacing: 2

                    Row {
                        spacing: Theme.spacingS

                        StyledText {
                            text: usage.reportReady ? (usage.isCost ? Fmt.currency(usage.report.cost) : Fmt.tokens(usage.report.processed)) : ""
                            font.pixelSize: 28
                            font.weight: Font.Medium
                            color: Theme.surfaceText
                        }

                        DIcon {
                            visible: usage.isCost && usage.reportReady && usage.report.unpriced > 0
                            name: "info"
                            size: 16
                            color: Theme.warning
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }

                    StyledText {
                        width: parent.width
                        text: {
                            if (!usage.reportReady)
                                return "";
                            const parts = [Fmt.sessions(usage.report.sessions)];
                            parts.push(usage.isCost ? "API estimate" : "processed tokens");
                            return parts.join(" · ");
                        }
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.surfaceVariantText
                    }

                    StyledText {
                        visible: usage.isCost && usage.reportReady && usage.report.unpriced > 0
                        width: parent.width
                        wrapMode: Text.WordWrap
                        text: usage.reportReady ? "The estimate leaves out " + Fmt.percent(usage.report.unpricedShare) + " of records, which have no price." : ""
                        font.pixelSize: Theme.fontSizeSmall - 1
                        color: Theme.warning
                    }
                }

                // Tokens by type (Tokens tab)
                Section {
                    visible: !usage.isCost
                    title: "Tokens by type"

                    StackBar {
                        width: parent.width
                        segments: usage.reportReady ? [
                            {
                                "label": "Input",
                                "value": usage.report.tokens.uncached,
                                "opacity": 0.6
                            },
                            {
                                "label": "Cache read",
                                "value": usage.report.tokens.cached,
                                "opacity": 0.3
                            },
                            {
                                "label": "Cache write",
                                "value": usage.report.tokens.creation,
                                "opacity": 0.72
                            },
                            {
                                "label": "Output",
                                "value": usage.report.tokens.output,
                                "opacity": 1
                            }
                        ] : []
                        format: v => Fmt.tokens(v)
                    }
                }

                // Cost by type (Cost tab)
                Section {
                    visible: usage.isCost
                    title: "Cost by type"

                    StackBar {
                        width: parent.width
                        segments: {
                            if (!usage.reportReady)
                                return [];
                            const t = usage.report.costByType;
                            const list = [
                                {
                                    "label": "Input",
                                    "value": t.input,
                                    "opacity": 0.6
                                },
                                {
                                    "label": "Cache read",
                                    "value": t.cacheRead,
                                    "opacity": 0.3
                                },
                                {
                                    "label": "Cache write",
                                    "value": t.cacheWrite,
                                    "opacity": 0.72
                                },
                                {
                                    "label": "Output",
                                    "value": t.output,
                                    "opacity": 1
                                }
                            ];
                            // Other is cost without a split; it hides below half a cent.
                            if (t.other >= 0.005)
                                list.push({
                                    "label": "Other",
                                    "value": t.other,
                                    "opacity": 0.44
                                });
                            return list;
                        }
                        format: v => Fmt.currency(v)
                    }
                }

                // By provider
                Section {
                    title: "By provider"

                    Repeater {
                        model: usage.reportReady ? usage.report.providers : []

                        delegate: Item {
                            required property var modelData
                            width: parent.width
                            height: 36

                            ProviderLogo {
                                id: logo
                                icon: modelData.icon
                                anchors.left: parent.left
                                anchors.verticalCenter: parent.verticalCenter
                            }

                            Column {
                                anchors.left: logo.right
                                anchors.leftMargin: Theme.spacingS
                                anchors.right: amounts.left
                                anchors.verticalCenter: parent.verticalCenter

                                StyledText {
                                    text: modelData.label
                                    font.pixelSize: Theme.fontSizeSmall + 1
                                    color: Theme.surfaceText
                                }

                                StyledText {
                                    text: Fmt.sessions(modelData.sessions) + " · " + Fmt.percent(usage.isCost ? modelData.costShare : modelData.tokenShare)
                                    font.pixelSize: Theme.fontSizeSmall - 1
                                    color: Theme.surfaceVariantText
                                }
                            }

                            Column {
                                id: amounts
                                anchors.right: parent.right
                                anchors.verticalCenter: parent.verticalCenter

                                StyledText {
                                    anchors.right: parent.right
                                    text: usage.isCost ? Fmt.currency(modelData.cost) : Fmt.tokens(modelData.tokens)
                                    font.pixelSize: Theme.fontSizeSmall + 1
                                    font.weight: Font.Medium
                                    color: Theme.surfaceText
                                }

                                StyledText {
                                    anchors.right: parent.right
                                    text: usage.isCost ? Fmt.tokens(modelData.tokens) + " tokens" : Fmt.currency(modelData.cost)
                                    font.pixelSize: Theme.fontSizeSmall - 1
                                    color: Theme.surfaceVariantText
                                }
                            }
                        }
                    }
                }

                // Chart
                Section {
                    title: {
                        if (!usage.reportReady)
                            return "";
                        const resolution = usage.report.hourly ? "Hourly" : "Daily";
                        return resolution + (usage.isCost ? " cost" : " tokens");
                    }

                    UsageChart {
                        width: parent.width
                        host: usage.host
                        report: usage.report
                        metric: usage.isCost ? "cost" : "tokens"
                    }
                }

                // Totals
                Section {
                    title: "Totals"

                    KeyValue {
                        label: "Processed tokens"
                        value: usage.reportReady ? Fmt.tokens(usage.report.processed) : ""
                    }
                    KeyValue {
                        label: "Cached input"
                        value: usage.reportReady ? Fmt.tokens(usage.report.tokens.cached) + (usage.report.cacheHit !== null ? "  (" + Fmt.percent(usage.report.cacheHit) + " hit)" : "") : ""
                    }
                    KeyValue {
                        label: "Uncached input"
                        value: usage.reportReady ? Fmt.tokens(usage.report.tokens.uncached + usage.report.tokens.creation) : ""
                    }
                    KeyValue {
                        label: "Output"
                        value: usage.reportReady ? Fmt.tokens(usage.report.tokens.output) : ""
                    }
                    KeyValue {
                        label: "Cache savings"
                        value: usage.reportReady ? Fmt.currency(usage.report.cacheSavings) : ""
                    }
                }

                // Cost by speed: only when something ran fast or ultrafast.
                Section {
                    visible: usage.isCost && usage.reportReady && (usage.report.costBySpeed.fast > 0 || usage.report.costBySpeed.ultrafast > 0)
                    title: "Cost by speed"

                    KeyValue {
                        label: "Standard"
                        value: usage.reportReady ? Fmt.currency(usage.report.costBySpeed.standard) : ""
                    }
                    KeyValue {
                        label: "Fast"
                        value: usage.reportReady ? Fmt.currency(usage.report.costBySpeed.fast) : ""
                    }
                    KeyValue {
                        label: "Ultrafast"
                        value: usage.reportReady ? Fmt.currency(usage.report.costBySpeed.ultrafast) : ""
                    }
                    KeyValue {
                        label: "Speed premium"
                        value: usage.reportReady ? Fmt.currency(usage.report.costBySpeed.premium) : ""
                    }
                }

                // Breakdown by model
                Section {
                    title: "Breakdown"

                    Repeater {
                        model: {
                            if (!usage.reportReady)
                                return [];
                            let models = usage.report.models.slice();
                            if (!usage.isCost)
                                models.sort((a, b) => b.tokens - a.tokens || b.cost - a.cost);
                            return usage.showAllModels ? models : models.slice(0, 5);
                        }

                        delegate: Item {
                            required property var modelData
                            width: parent.width
                            height: 24

                            ProviderLogo {
                                id: modelLogo
                                icon: modelData.icon
                                size: 13
                                anchors.left: parent.left
                                anchors.verticalCenter: parent.verticalCenter
                            }

                            StyledText {
                                anchors.left: modelLogo.right
                                anchors.leftMargin: Theme.spacingS
                                anchors.right: modelValue.left
                                anchors.rightMargin: Theme.spacingS
                                anchors.verticalCenter: parent.verticalCenter
                                text: modelData.name
                                font.pixelSize: Theme.fontSizeSmall
                                color: Theme.surfaceText
                                elide: Text.ElideRight
                            }

                            StyledText {
                                id: modelValue
                                anchors.right: parent.right
                                anchors.verticalCenter: parent.verticalCenter
                                text: usage.isCost ? (modelData.unpriced ? "Unpriced" : Fmt.currency(modelData.cost)) : Fmt.tokens(modelData.tokens)
                                font.pixelSize: Theme.fontSizeSmall
                                color: modelData.unpriced && usage.isCost ? Theme.surfaceVariantText : Theme.surfaceText
                            }
                        }
                    }

                    StyledText {
                        visible: usage.reportReady && usage.report.models.length > 5
                        text: usage.showAllModels ? "Show fewer" : "Show all " + (usage.reportReady ? usage.report.models.length : 0) + " models"
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.primary

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: usage.showAllModels = !usage.showAllModels
                        }
                    }
                }

                // Footer: warnings and cache age.
                StyledText {
                    width: parent.width
                    wrapMode: Text.WordWrap
                    font.pixelSize: Theme.fontSizeSmall - 2
                    color: Theme.surfaceVariantText
                    opacity: 0.8
                    text: {
                        if (!usage.reportReady)
                            return "";
                        const lines = [];
                        if (usage.report.rejectedFiles > 0)
                            lines.push(usage.report.rejectedFiles + " cache entries could not be read; totals may read low.");
                        if (usage.report.malformed > 0)
                            lines.push(usage.report.malformed + " transcript lines could not be read.");
                        lines.push("Transcript cache updated " + usage.report.cacheAge + ". Costs are API estimates from T3 Code's price list, not what a plan bills.");
                        return lines.join("\n");
                    }
                }
            }

            // ===== Limits =====

            Message {
                visible: usage.tab === "limits" && !usage.limits
                text: "Reading limits…"
            }

            Message {
                visible: usage.tab === "limits" && !!usage.limits && usage.limits.state !== "ok"
                text: usage.limits ? (usage.limits.state === "noCacheDirectory" ? "No limits data yet. Open T3 Code once so it writes its provider files." : "No limits reported.") : ""
            }

            Column {
                visible: usage.tab === "limits" && !!usage.limits && usage.limits.state === "ok"
                width: parent.width
                spacing: Theme.spacingM

                StyledText {
                    visible: !!usage.limits && !!usage.limits.staleness
                    width: parent.width
                    wrapMode: Text.WordWrap
                    text: usage.limits && usage.limits.staleness ? usage.limits.staleness : ""
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.warning
                }

                Repeater {
                    model: usage.limits ? usage.limits.groups : []

                    delegate: LimitsGroup {
                        required property var modelData
                        width: parent.width
                        group: modelData
                    }
                }

                Repeater {
                    model: usage.limits ? usage.limits.notices : []

                    delegate: StyledText {
                        required property var modelData
                        width: parent.width
                        wrapMode: Text.WordWrap
                        text: modelData
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.surfaceVariantText
                    }
                }

                Repeater {
                    model: usage.limits ? usage.limits.links : []

                    delegate: Rectangle {
                        required property var modelData
                        width: parent.width
                        height: linkColumn.implicitHeight + Theme.spacingM * 2
                        radius: Theme.cornerRadius
                        color: Theme.withAlpha(Theme.surfaceText, 0.05)

                        Column {
                            id: linkColumn
                            x: Theme.spacingM
                            y: Theme.spacingM
                            width: parent.width - Theme.spacingM * 2
                            spacing: Theme.spacingXS

                            StyledText {
                                text: modelData.label
                                font.pixelSize: Theme.fontSizeSmall + 1
                                font.weight: Font.Medium
                                color: Theme.surfaceText
                            }

                            StyledText {
                                visible: !!modelData.body
                                width: parent.width
                                wrapMode: Text.WordWrap
                                text: modelData.body || ""
                                font.pixelSize: Theme.fontSizeSmall
                                color: Theme.surfaceVariantText
                            }

                            StyledText {
                                visible: !!modelData.url
                                text: "Manage usage"
                                font.pixelSize: Theme.fontSizeSmall
                                font.weight: Font.Medium
                                color: Theme.primary

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: usage.host.openUrl(modelData.url)
                                }
                            }
                        }
                    }
                }

                StyledText {
                    width: parent.width
                    wrapMode: Text.WordWrap
                    text: (usage.limits && usage.limits.skippedFiles > 0 ? usage.limits.skippedFiles + " provider " + (usage.limits.skippedFiles === 1 ? "file" : "files") + " could not be read.\n" : "") + (usage.limits ? usage.limits.footnote : "")
                    font.pixelSize: Theme.fontSizeSmall - 2
                    color: Theme.surfaceVariantText
                    opacity: 0.8
                }
            }
        }
    }

    // ----- building blocks -----

    component Message: StyledText {
        width: parent ? parent.width : 0
        wrapMode: Text.WordWrap
        font.pixelSize: Theme.fontSizeSmall
        color: Theme.surfaceVariantText
        topPadding: Theme.spacingM
        bottomPadding: Theme.spacingM
        horizontalAlignment: Text.AlignHCenter
    }

    component Section: Column {
        id: section
        property string title
        default property alias items: sectionBody.data
        width: parent ? parent.width : 0
        spacing: Theme.spacingS

        StyledText {
            text: section.title
            font.pixelSize: Theme.fontSizeSmall - 1
            font.weight: Font.Medium
            font.capitalization: Font.AllUppercase
            font.letterSpacing: 0.6
            color: Theme.surfaceVariantText
        }

        Column {
            id: sectionBody
            width: parent.width
            spacing: 2
        }
    }

    component KeyValue: Item {
        id: kv
        property string label
        property string value
        width: parent ? parent.width : 0
        height: 22

        StyledText {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: kv.label
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.surfaceVariantText
        }

        StyledText {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: kv.value
            font.pixelSize: Theme.fontSizeSmall
            font.features: {
                "tnum": 1
            }
            color: Theme.surfaceText
        }
    }

    component ProviderLogo: Item {
        id: providerLogo
        property string icon
        property real size: 18
        width: size
        height: size

        Image {
            anchors.fill: parent
            visible: providerLogo.icon !== ""
            source: providerLogo.icon ? Qt.resolvedUrl("assets/providers/" + providerLogo.icon + (providerLogo.icon === "antigravity" ? ".png" : ".svg")) : ""
            sourceSize: Qt.size(providerLogo.size * 2, providerLogo.size * 2)
            fillMode: Image.PreserveAspectFit
            smooth: true
        }

        DIcon {
            visible: providerLogo.icon === ""
            anchors.centerIn: parent
            name: "smart_toy"
            size: providerLogo.size
            color: Theme.surfaceVariantText
        }
    }

    // A proportional bar with a legend under it, one row per segment.
    component StackBar: Column {
        id: stack
        property var segments: []
        property var format: v => String(v)
        readonly property real total: segments.reduce((sum, s) => sum + Math.max(0, s.value), 0)
        spacing: Theme.spacingS

        Row {
            width: parent.width
            height: 8
            spacing: 2

            Repeater {
                model: stack.segments

                delegate: Rectangle {
                    required property var modelData
                    required property int index
                    readonly property real share: stack.total > 0 ? Math.max(0, modelData.value) / stack.total : 0
                    visible: share > 0
                    width: Math.max(2, share * (stack.width - 2 * (stack.segments.length - 1)))
                    height: 8
                    radius: 2
                    color: Theme.withAlpha(Theme.surfaceText, modelData.opacity)
                }
            }
        }

        Repeater {
            model: stack.segments

            delegate: Item {
                required property var modelData
                width: stack.width
                height: 20

                Rectangle {
                    id: swatch
                    width: 8
                    height: 8
                    radius: 2
                    color: Theme.withAlpha(Theme.surfaceText, modelData.opacity)
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                }

                StyledText {
                    anchors.left: swatch.right
                    anchors.leftMargin: Theme.spacingS
                    anchors.verticalCenter: parent.verticalCenter
                    text: modelData.label
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.surfaceVariantText
                }

                StyledText {
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    text: stack.format(modelData.value) + (stack.total > 0 ? "  " + Fmt.percent(Math.max(0, modelData.value) / stack.total) : "")
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.surfaceText
                }
            }
        }
    }

    component Segmented: Rectangle {
        id: seg
        property var options: []
        property var current
        property bool compact: false
        signal picked(var value)
        width: segRow.implicitWidth + 4
        height: 28
        radius: 14
        color: Theme.withAlpha(Theme.surfaceText, 0.06)

        Row {
            id: segRow
            x: 2
            y: 2
            spacing: 2

            Repeater {
                model: seg.options

                delegate: Rectangle {
                    required property var modelData
                    readonly property bool selected: modelData.value === seg.current
                    width: segLabel.implicitWidth + (seg.compact ? 14 : 20)
                    height: 24
                    radius: 12
                    color: selected ? Theme.primary : (segArea.containsMouse ? Theme.withAlpha(Theme.surfaceText, 0.08) : "transparent")

                    StyledText {
                        id: segLabel
                        anchors.centerIn: parent
                        text: modelData.label
                        font.pixelSize: Theme.fontSizeSmall
                        font.weight: Font.Medium
                        color: parent.selected ? Theme.primaryText : Theme.surfaceVariantText
                    }

                    MouseArea {
                        id: segArea
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: seg.picked(modelData.value)
                    }
                }
            }
        }
    }
}
