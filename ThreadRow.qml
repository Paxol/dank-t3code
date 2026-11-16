import QtQuick
import qs.Common
import qs.DCommon.Widgets
import "T3Fmt.js" as Fmt

// One thread, laid out like T3 Code's sidebar:
//   [badge] [pin] project ............ status or idle time
//   title ............................ provider logos      (no branch)
//   title / branch ................... provider logos      (with branch)
// Resting the pointer for half a second asks the popout to float the start of the last reply.
Rectangle {
    id: row

    required property var host
    required property var thread
    property bool liveStatus: true

    signal activated
    signal menuRequested(Item item, real x, real y)
    signal previewRequested(Item item)
    signal previewDismissed

    readonly property string status: liveStatus ? thread.status : "settled"
    readonly property bool hasBranch: (thread.branch || "").length > 0
    readonly property bool hovered: rowArea.containsMouse

    implicitHeight: body.implicitHeight + 14
    radius: Theme.cornerRadius
    color: hovered ? Theme.withAlpha(Theme.surfaceText, 0.07) : "transparent"

    Timer {
        id: hoverTimer
        interval: 500
        onTriggered: {
            row.host.loadPreview(row.thread);
            row.previewRequested(row);
        }
    }

    onHoveredChanged: {
        if (hovered && host.showPreviews) {
            hoverTimer.restart();
        } else {
            hoverTimer.stop();
            previewDismissed();
        }
    }

    MouseArea {
        id: rowArea
        anchors.fill: parent
        hoverEnabled: true
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        cursorShape: Qt.PointingHandCursor
        onClicked: mouse => {
            if (mouse.button === Qt.RightButton)
                row.menuRequested(row, mouse.x, mouse.y);
            else
                row.activated();
        }
    }

    Column {
        id: body
        x: 10
        y: 7
        width: parent.width - 20
        spacing: 3

        // Line 1: badge, pin, project, status
        Item {
            width: parent.width
            height: 18

            Row {
                anchors.left: parent.left
                anchors.right: statusSlot.left
                anchors.rightMargin: Theme.spacingS
                anchors.verticalCenter: parent.verticalCenter
                spacing: 6

                ProjectBadge {
                    badge: row.thread.badge
                    anchors.verticalCenter: parent.verticalCenter
                }

                DIcon {
                    visible: row.thread.isPinned
                    name: "keep"
                    size: 12
                    filled: true
                    color: Theme.surfaceVariantText
                    anchors.verticalCenter: parent.verticalCenter
                }

                StyledText {
                    width: Math.min(implicitWidth, parent.width - 22 - (row.thread.isPinned ? 18 : 0) - (row.thread.remote ? 20 : 0))
                    text: row.thread.projectTitle
                    font.pixelSize: Theme.fontSizeSmall
                    color: row.hovered ? Theme.surfaceText : Theme.surfaceVariantText
                    elide: Text.ElideRight
                    anchors.verticalCenter: parent.verticalCenter
                }

                // Only rows from another environment show where they run.
                DIcon {
                    visible: !!row.thread.remote
                    name: Fmt.machineIcon(row.thread.machine)
                    size: 13
                    color: Theme.surfaceVariantText
                    opacity: 0.8
                    anchors.verticalCenter: parent.verticalCenter
                }
            }

            Item {
                id: statusSlot
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                width: row.status === "settled" ? idleText.implicitWidth : pill.implicitWidth
                height: parent.height

                StyledText {
                    id: idleText
                    visible: row.status === "settled"
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    text: Fmt.relative(row.thread.lastActivityAt, row.host.now)
                    font.pixelSize: Theme.fontSizeSmall - 1
                    color: Theme.surfaceVariantText
                    opacity: 0.85
                }

                StatusPill {
                    id: pill
                    visible: row.status !== "settled"
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    host: row.host
                    status: row.status
                    suffix: row.host.pillSuffix(row.thread)
                }
            }
        }

        // Line 2: title (and providers when there is no branch)
        Item {
            width: parent.width
            height: titleText.implicitHeight

            StyledText {
                id: titleText
                anchors.left: parent.left
                anchors.right: row.hasBranch ? parent.right : providersTop.left
                anchors.rightMargin: row.hasBranch ? 0 : Theme.spacingS
                text: row.thread.title
                font.pixelSize: Theme.fontSizeSmall + 1
                font.weight: Font.Medium
                color: Theme.surfaceText
                elide: Text.ElideRight
            }

            ProviderIcons {
                id: providersTop
                visible: !row.hasBranch
                providers: row.thread.providers
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
            }
        }

        // Line 3: branch and providers
        Item {
            visible: row.hasBranch
            width: parent.width
            height: branchText.implicitHeight

            DIcon {
                id: branchIcon
                name: "call_split"
                size: 11
                color: Theme.surfaceVariantText
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
            }

            StyledText {
                id: branchText
                anchors.left: branchIcon.right
                anchors.leftMargin: 3
                anchors.right: providersBottom.left
                anchors.rightMargin: Theme.spacingS
                text: row.thread.branch + (row.thread.prNumber ? "  #" + row.thread.prNumber : "")
                font.pixelSize: Theme.fontSizeSmall - 1
                color: Theme.surfaceVariantText
                elide: Text.ElideRight
            }

            ProviderIcons {
                id: providersBottom
                providers: row.thread.providers
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }

    Accessible.role: Accessible.Button
    Accessible.name: thread.title + ", in " + thread.projectTitle + (thread.remote ? ", on " + (thread.environmentLabel || Fmt.machineName(thread.machine)) : "") + (hasBranch ? ", on branch " + thread.branch : "") + (status !== "settled" ? ", " + host.statusLabel(status) : "")
}
