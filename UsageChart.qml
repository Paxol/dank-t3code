import QtQuick
import qs.Common
import qs.DCommon.Widgets
import "T3Fmt.js" as Fmt

// Daily (or, for Past 24h, hourly) cost or tokens per provider, stacked.
// Hovering a column shows its numbers in the caption above the bars.
Column {
    id: chart

    required property var host
    property var report: null
    property string metric: "cost"
    property int hovered: -1

    readonly property var columns: report && report.chart ? report.chart : []
    readonly property var providers: report && report.providers ? report.providers : []
    readonly property real maxTotal: {
        let max = 0;
        for (const column of columns) {
            let total = 0;
            for (const v of column.values)
                total += metric === "cost" ? v.cost : v.tokens;
            max = Math.max(max, total);
        }
        return max;
    }

    function value(v) {
        return metric === "cost" ? v.cost : v.tokens;
    }

    function format(v) {
        return metric === "cost" ? Fmt.currency(v) : Fmt.tokens(v);
    }

    function accent(kind) {
        for (const p of providers)
            if (p.kind === kind)
                return p.accent || Theme.surfaceText;
        return Theme.surfaceText;
    }

    function caption() {
        if (hovered < 0 || hovered >= columns.length)
            return "Peak " + format(maxTotal);
        const column = columns[hovered];
        let total = 0;
        const parts = [];
        for (const v of column.values) {
            total += value(v);
            if (value(v) > 0)
                for (const p of providers)
                    if (p.kind === v.kind)
                        parts.push(p.label + " " + format(value(v)));
        }
        return Fmt.columnLabel(column.start, report.hourly) + " · " + format(total) + (parts.length > 1 ? "  (" + parts.join(", ") + ")" : "");
    }

    spacing: Theme.spacingXS

    StyledText {
        width: parent.width
        text: chart.caption()
        font.pixelSize: Theme.fontSizeSmall - 1
        color: chart.hovered >= 0 ? Theme.surfaceText : Theme.surfaceVariantText
        elide: Text.ElideRight
    }

    Item {
        width: parent.width
        height: 72

        // Baseline
        Rectangle {
            width: parent.width
            height: 1
            anchors.bottom: parent.bottom
            color: Theme.withAlpha(Theme.outline, 0.3)
        }

        Row {
            id: bars
            anchors.fill: parent
            spacing: chart.columns.length > 40 ? 1 : 2
            readonly property real barWidth: chart.columns.length ? (width - spacing * (chart.columns.length - 1)) / chart.columns.length : 0

            Repeater {
                model: chart.columns

                delegate: Item {
                    id: bar
                    required property var modelData
                    required property int index
                    width: bars.barWidth
                    height: bars.height
                    opacity: chart.hovered < 0 || chart.hovered === index ? 1 : 0.45

                    Rectangle {
                        anchors.fill: parent
                        color: chart.hovered === bar.index ? Theme.withAlpha(Theme.surfaceText, 0.06) : "transparent"
                        radius: 2
                    }

                    Column {
                        anchors.bottom: parent.bottom
                        width: parent.width

                        Repeater {
                            // Bottom-up stacking: reverse so the first provider sits on the baseline.
                            model: bar.modelData.values.slice().reverse()

                            delegate: Rectangle {
                                required property var modelData
                                width: bar.width
                                height: chart.maxTotal > 0 ? Math.max(0, chart.value(modelData) / chart.maxTotal * (bars.height - 2)) : 0
                                color: chart.accent(modelData.kind)
                                radius: Math.min(2, width / 2)
                            }
                        }
                    }

                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        onContainsMouseChanged: {
                            if (containsMouse)
                                chart.hovered = bar.index;
                            else if (chart.hovered === bar.index)
                                chart.hovered = -1;
                        }
                    }
                }
            }
        }
    }

    // Axis: first and last column.
    Item {
        width: parent.width
        height: axisStart.implicitHeight

        StyledText {
            id: axisStart
            anchors.left: parent.left
            text: chart.columns.length ? Fmt.columnLabel(chart.columns[0].start, chart.report.hourly).split("–")[0] : ""
            font.pixelSize: Theme.fontSizeSmall - 2
            color: Theme.surfaceVariantText
        }

        StyledText {
            anchors.right: parent.right
            text: chart.columns.length ? Fmt.columnLabel(chart.columns[chart.columns.length - 1].start, chart.report.hourly).split("–")[0] : ""
            font.pixelSize: Theme.fontSizeSmall - 2
            color: Theme.surfaceVariantText
        }
    }

    // Legend
    Flow {
        width: parent.width
        spacing: Theme.spacingM
        visible: chart.providers.length > 1

        Repeater {
            model: chart.providers

            delegate: Row {
                required property var modelData
                spacing: 4

                Rectangle {
                    width: 8
                    height: 8
                    radius: 2
                    color: modelData.accent || Theme.surfaceText
                    anchors.verticalCenter: parent.verticalCenter
                }

                StyledText {
                    text: modelData.label
                    font.pixelSize: Theme.fontSizeSmall - 1
                    color: Theme.surfaceVariantText
                }
            }
        }
    }
}
