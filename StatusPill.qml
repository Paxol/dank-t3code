import QtQuick
import qs.Common
import qs.DCommon.Widgets

// T3 Code's sidebar status: an icon and a medium-weight label in one color,
// with an optional trailing duration.
Row {
    id: pill

    required property var host
    property string status: "settled"
    property string suffix: ""

    readonly property color tint: host.statusColor(status)

    spacing: 4

    DIcon {
        id: icon
        name: pill.host.statusIcon(pill.status)
        size: 14
        color: pill.tint
        anchors.verticalCenter: parent.verticalCenter

        RotationAnimator on rotation {
            running: pill.status === "working" || pill.status === "goal"
            from: 0
            to: 360
            duration: 2400
            loops: Animation.Infinite
            onStopped: icon.rotation = 0
        }
    }

    StyledText {
        text: pill.host.statusLabel(pill.status) + (pill.suffix ? " " + pill.suffix : "")
        font.pixelSize: Theme.fontSizeSmall
        font.weight: Font.Medium
        font.features: {
            "tnum": 1
        }
        color: pill.tint
        anchors.verticalCenter: parent.verticalCenter
    }
}
