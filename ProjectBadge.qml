import QtQuick
import qs.Common
import qs.DCommon.Widgets

// T3 Code's project mark: its icon, emoji or favicon, else a colored monogram.
Item {
    id: mark

    property var badge: ({})
    property real size: 16

    implicitWidth: size
    implicitHeight: size

    readonly property string kind: badge && badge.kind ? badge.kind : "monogram"

    Rectangle {
        visible: mark.kind === "monogram"
        anchors.fill: parent
        radius: 4
        color: Theme.withAlpha(mark.badge.color || "#60a5fa", 0.14)
        border.width: 1
        border.color: Theme.withAlpha(mark.badge.color || "#60a5fa", 0.3)

        StyledText {
            anchors.centerIn: parent
            text: mark.badge.text || ""
            font.pixelSize: Math.round(mark.size * 0.5)
            font.weight: Font.Bold
            color: mark.badge.color || "#60a5fa"
        }
    }

    StyledText {
        visible: mark.kind === "emoji"
        anchors.centerIn: parent
        text: mark.badge.text || ""
        font.pixelSize: Math.round(mark.size * 0.72)
    }

    DIcon {
        visible: mark.kind === "none" || mark.kind === "folder"
        anchors.centerIn: parent
        name: mark.kind === "folder" ? "folder" : "chat_bubble"
        size: mark.size - 2
        color: mark.badge.color || "#9ca3af"
    }

    Image {
        visible: mark.kind === "image"
        anchors.fill: parent
        source: mark.kind === "image" ? "file://" + mark.badge.path : ""
        sourceSize: Qt.size(mark.size * 2, mark.size * 2)
        fillMode: Image.PreserveAspectFit
        asynchronous: true
        smooth: true
    }
}
