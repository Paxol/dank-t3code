import QtQuick
import qs.Common
import qs.DCommon.Widgets

// The live connection to the local T3 server (LimitsConnectionView in the macOS app):
// its status, and a form to connect with a new read-only pairing code or an access
// token from T3's CLI. The connection also feeds the thread list and, with the
// usage scope, the Cost and Tokens tabs.
Column {
    id: panel

    required property var host
    property var live: null

    property bool expanded: false
    property bool remember: false

    readonly property string status: live ? live.status : ""
    readonly property bool connected: !!live && live.connected
    // A remote environment's connection is managed on the Environments screen.
    readonly property bool remote: !!live && !!live.remote
    readonly property color tone: connected ? Theme.success : (status === "failed" ? Theme.error : (live && live.retained || status === "retrying" || status === "offline" ? Theme.warning : Theme.surfaceVariantText))

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
        if (remote)
            return live.label + ": " + (connected ? "live" + (live.lastUpdatedAgo ? " · updated " + live.lastUpdatedAgo : "") : live.text);
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
                name: panel.connected ? "sensors" : (panel.status === "connecting" || panel.status === "retrying" || panel.status === "offline" ? "sync" : (panel.status === "failed" ? "error" : "cloud_off"))
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
                text: panel.remote ? "Manage…" : (panel.connected || (panel.live && panel.live.retained) ? "Disconnect" : (panel.expanded ? "Hide" : "Connect…"))
                font.pixelSize: Theme.fontSizeSmall
                font.weight: Font.Medium
                color: Theme.primary
                anchors.verticalCenter: parent.verticalCenter

                MouseArea {
                    anchors.fill: parent
                    anchors.margins: -4
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        if (panel.remote) {
                            panel.host.screen = "environments";
                        } else if (panel.connected || (panel.live && panel.live.retained)) {
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

    // Pairing form
    Column {
        visible: panel.expanded && !panel.connected && !panel.remote
        width: parent.width
        spacing: Theme.spacingS

        StyledText {
            width: parent.width
            wrapMode: Text.WordWrap
            text: "Paste a pairing code or an access token. For a code, in T3 Code: Settings → Connections → Authorized clients → Create link. Under Permissions keep only View environment, plus View diagnostics and usage for live cost and tokens, then copy the code. A code gives a token for 30 days."
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.surfaceVariantText
        }

        StyledText {
            width: parent.width
            wrapMode: Text.WrapAnywhere
            text: "For a token that lasts longer, run: t3 auth session issue --scope orchestration:read --scope diagnostics:read --ttl 365d --label \"T3 Code Bar\" --token-only"
            font.pixelSize: Theme.fontSizeSmall - 1
            font.family: "monospace"
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
                labelText: "Pairing code or access token"
                placeholderText: "Code or token"
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
                text: "Connect"
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
            text: "Read-only · This computer only. Connects to 127.0.0.1. Other machines are added on the Environments screen."
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
