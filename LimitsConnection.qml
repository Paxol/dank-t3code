import QtQuick
import qs.Common
import qs.DCommon.Widgets

// The live connection to the local T3 server (LimitsConnectionView in the macOS app):
// its status, and a form to pair with a new read-only code. The connection also
// feeds the thread list and, with the usage scope, the Cost and Tokens tabs.
Column {
    id: panel

    required property var host
    property var live: null

    property bool expanded: false
    property bool remember: false

    readonly property string status: live ? live.status : ""
    readonly property bool connected: !!live && live.connected
    readonly property color tone: connected ? Theme.success : (status === "failed" ? Theme.error : (live && live.retained || status === "retrying" ? Theme.warning : Theme.surfaceVariantText))

    visible: !!live
    spacing: Theme.spacingS


    onConnectedChanged: {
        if (connected) {
            expanded = false;
            codeField.text = "";
        }
    }

    function headline() {
        if (!live)
            return "";
        if (connected) {
            const parts = [];
            if (live.threads)
                parts.push("threads");
            parts.push("limits");
            if (live.usage)
                parts.push("cost and tokens");
            const list = parts.length > 1 ? parts.slice(0, -1).join(", ") + " and " + parts[parts.length - 1] : parts[0];
            return "Live from T3 Code: " + list + (live.lastUpdatedAgo ? " · updated " + live.lastUpdatedAgo : "");
        }
        if (live.retained)
            return "Disconnected · showing data from " + (live.lastUpdatedAgo || "earlier") + " and saved checks";
        return live.text;
    }

    function pair() {
        const code = codeField.text;
        codeField.text = "";
        host.sendLiveCommand({
            "cmd": "pair",
            "port": parseInt(portField.text, 10) || 0,
            "code": code,
            "remember": panel.remember
        });
    }

    Rectangle {
        width: parent.width
        height: statusRow.implicitHeight + Theme.spacingS * 2
        radius: Theme.cornerRadius
        color: Theme.withAlpha(panel.tone, 0.1)

        Row {
            id: statusRow
            x: Theme.spacingS
            y: Theme.spacingS
            width: parent.width - Theme.spacingS * 2
            spacing: Theme.spacingS

            DIcon {
                id: statusIcon
                name: panel.connected ? "sensors" : (panel.status === "connecting" || panel.status === "retrying" ? "sync" : (panel.status === "failed" ? "error" : "cloud_off"))
                size: 16
                color: panel.tone
                anchors.verticalCenter: parent.verticalCenter
            }

            StyledText {
                width: parent.width - statusIcon.width - actionText.width - Theme.spacingS * 2
                text: panel.headline()
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.surfaceText
                wrapMode: Text.WordWrap
                anchors.verticalCenter: parent.verticalCenter
            }

            StyledText {
                id: actionText
                text: panel.connected || (panel.live && panel.live.retained) ? "Disconnect" : (panel.expanded ? "Hide" : "Connect…")
                font.pixelSize: Theme.fontSizeSmall
                font.weight: Font.Medium
                color: Theme.primary
                anchors.verticalCenter: parent.verticalCenter

                MouseArea {
                    anchors.fill: parent
                    anchors.margins: -4
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        if (panel.connected || (panel.live && panel.live.retained)) {
                            panel.host.sendLiveCommand({
                                "cmd": "disconnect"
                            });
                        } else {
                            panel.expanded = !panel.expanded;
                            if (!panel.expanded)
                                codeField.text = "";
                        }
                    }
                }
            }
        }
    }

    StyledText {
        visible: !!panel.live && !panel.live.available
        width: parent.width
        wrapMode: Text.WordWrap
        text: "Install the Python cryptography package (python-cryptography) to connect live."
        font.pixelSize: Theme.fontSizeSmall
        color: Theme.warning
    }

    // Pairing form
    Column {
        visible: panel.expanded && !panel.connected
        width: parent.width
        spacing: Theme.spacingS

        StyledText {
            width: parent.width
            wrapMode: Text.WordWrap
            text: "In T3 Code: Settings → Connections → Authorized clients → Create link. Under Permissions keep only View environment, plus View diagnostics and usage for live cost and tokens, then copy the code."
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.surfaceVariantText
        }

        Row {
            width: parent.width
            spacing: Theme.spacingS

            DTextField {
                id: portField
                width: 90
                labelText: "Local port"
                text: panel.host.inbox && panel.host.inbox.port ? String(panel.host.inbox.port) : ""
                validator: IntValidator {
                    bottom: 1
                    top: 65535
                }
            }

            DTextField {
                id: codeField
                width: parent.width - portField.width - Theme.spacingS
                labelText: "Pairing code"
                placeholderText: "New read-only code"
                echoMode: TextInput.Password
                showPasswordToggle: true
                onAccepted: {
                    if (text.trim().length > 0)
                        panel.pair();
                }
            }
        }

        DToggle {
            width: parent.width
            text: "Remember in keyring"
            description: panel.live && !panel.live.keyring ? "secret-tool is not installed" : "Reconnect automatically after a restart"
            checked: panel.remember
            enabled: !!panel.live && panel.live.keyring
            onToggled: value => panel.remember = value
        }

        Row {
            spacing: Theme.spacingS

            ActionButton {
                text: "Pair & connect"
                primary: true
                enabled: codeField.text.trim().length > 0
                onClicked: panel.pair()
            }

            ActionButton {
                text: "Reconnect saved"
                onClicked: panel.host.sendLiveCommand({
                    "cmd": "reconnectSaved",
                    "port": parseInt(portField.text, 10) || 0
                })
            }
        }

        StyledText {
            width: parent.width
            wrapMode: Text.WordWrap
            text: "Read-only · This computer only. Connects to 127.0.0.1; linked environments are not included."
            font.pixelSize: Theme.fontSizeSmall - 2
            color: Theme.surfaceVariantText
            opacity: 0.8
        }
    }

    component ActionButton: Rectangle {
        id: button
        property alias text: buttonLabel.text
        property bool primary: false
        signal clicked
        width: buttonLabel.implicitWidth + Theme.spacingM * 2
        height: 30
        radius: 15
        opacity: enabled ? 1 : 0.45
        color: primary ? (buttonArea.containsMouse ? Qt.lighter(Theme.primary, 1.1) : Theme.primary) : (buttonArea.containsMouse ? Theme.withAlpha(Theme.primary, 0.18) : Theme.withAlpha(Theme.primary, 0.1))

        StyledText {
            id: buttonLabel
            anchors.centerIn: parent
            font.pixelSize: Theme.fontSizeSmall
            font.weight: Font.Medium
            color: button.primary ? Theme.primaryText : Theme.primary
        }

        MouseArea {
            id: buttonArea
            anchors.fill: parent
            hoverEnabled: true
            enabled: button.enabled
            cursorShape: Qt.PointingHandCursor
            onClicked: button.clicked()
        }
    }
}
