import QtQuick
import qs.Common
import qs.DCommon.Widgets

// The logos of the providers that ran in a thread, as T3 Code shows them.
Row {
    id: icons

    property var providers: []
    property real size: 13

    spacing: 4

    Repeater {
        model: icons.providers

        delegate: Item {
            required property var modelData
            width: icons.size
            height: icons.size

            Image {
                visible: modelData.kind !== "unknown"
                anchors.fill: parent
                source: modelData.kind === "unknown" ? "" : Qt.resolvedUrl("assets/providers/" + modelData.kind + (modelData.kind === "antigravity" ? ".png" : ".svg"))
                sourceSize: Qt.size(icons.size * 2, icons.size * 2)
                fillMode: Image.PreserveAspectFit
                smooth: true
            }

            StyledText {
                visible: modelData.kind === "unknown"
                anchors.centerIn: parent
                text: modelData.initials || "?"
                font.pixelSize: Math.round(icons.size * 0.55)
                font.weight: Font.DemiBold
                color: Theme.surfaceVariantText
            }
        }
    }
}
