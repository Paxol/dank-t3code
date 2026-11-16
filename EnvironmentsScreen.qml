import QtQuick
import qs.Common
import qs.DCommon.Widgets
import "T3Fmt.js" as Fmt

// Local and the paired remote environments: their status, Forget access and Remove, and a
// pairing link to add one. The live process does the pairing; its reply arrives in
// host.pairingLive.
Column {
    id: screen

    required property var host

    property real pendingSeq: 0
    property string result: ""
    property bool resultOk: false
    property string confirmRemove: ""

    readonly property var info: host.inbox && host.inbox.environmentInfo ? host.inbox.environmentInfo : ({})

    spacing: Theme.spacingM

    Connections {
        target: screen.host
        function onPairingLiveChanged() {
            const reply = screen.host.pairingLive;
            if (!reply || !screen.pendingSeq || reply.seq !== screen.pendingSeq)
                return;
            screen.pendingSeq = 0;
            screen.resultOk = !!reply.ok;
            if (!reply.ok)
                screen.result = reply.error || "Could not connect.";
            else if (reply.local)
                screen.result = "This link is for this computer's T3 Code, so the local connection is pairing with it.";
            else
                screen.result = "Connected to " + reply.label + "." + (reply.remembered === false ? " The keyring is not available, so pair again after a restart." : "");
        }
    }

    // Identifying the server and redeeming the code each take up to 15 seconds.
    Timer {
        id: pairingTimeout
        interval: 45000
        onTriggered: {
            if (!screen.pendingSeq)
                return;
            screen.pendingSeq = 0;
            screen.resultOk = false;
            screen.result = "No answer. Check the address and try again with a new link.";
        }
    }

    function addEnvironment() {
        const link = linkField.text;
        linkField.text = "";
        result = "";
        pendingSeq = Date.now();
        pairingTimeout.restart();
        host.sendLiveCommand({
            "cmd": "addEnvironment",
            "link": link,
            "seq": pendingSeq
        });
    }

    function localText(env) {
        switch (env.state) {
        case "online":
            return "This computer · live connection";
        case "offline":
            return "This computer · reading T3 Code's local files";
        case "unavailable":
            return "This computer · no local T3 Code data";
        }
        return "This computer";
    }

    function stateColor(state) {
        if (state === "online")
            return Theme.success;
        if (state === "offline" || state === "connecting")
            return Theme.warning;
        if (state === "failed")
            return Theme.error;
        return Theme.surfaceVariantText;
    }

    StyledText {
        width: parent.width
        wrapMode: Text.WordWrap
        text: "Threads, usage and limits from T3 Code on other machines. Pick one environment or All from the picker under the header."
        font.pixelSize: Theme.fontSizeSmall
        color: Theme.surfaceVariantText
    }

    Rectangle {
        visible: !!screen.info.registryError
        width: parent.width
        height: registryText.implicitHeight + Theme.spacingS * 2
        radius: Theme.cornerRadius
        color: Theme.withAlpha(Theme.error, 0.1)

        StyledText {
            id: registryText
            x: Theme.spacingS
            y: Theme.spacingS
            width: parent.width - Theme.spacingS * 2
            wrapMode: Text.WordWrap
            text: screen.info.registryError || ""
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.surfaceText
        }
    }

    // ----- environments -----

    Column {
        width: parent.width
        spacing: Theme.spacingXS

        Repeater {
            model: screen.host.environments

            Rectangle {
                id: envRow
                required property var modelData
                readonly property bool remote: !modelData.local
                readonly property bool hasGrant: remote && modelData.state !== "needsAccess"

                width: parent.width
                height: envColumn.implicitHeight + Theme.spacingS * 2
                radius: Theme.cornerRadius
                color: Theme.withAlpha(Theme.surfaceText, 0.04)

                Column {
                    id: envColumn
                    x: Theme.spacingS
                    y: Theme.spacingS
                    width: parent.width - Theme.spacingS * 2
                    spacing: 4

                    Row {
                        width: parent.width
                        spacing: Theme.spacingS

                        DIcon {
                            name: envRow.remote ? Fmt.machineIcon(envRow.modelData.machine) : "computer"
                            size: 18
                            color: Theme.surfaceVariantText
                            anchors.verticalCenter: parent.verticalCenter
                        }

                        StyledText {
                            width: Math.min(implicitWidth, parent.width - 60)
                            text: envRow.modelData.label
                            elide: Text.ElideRight
                            font.pixelSize: Theme.fontSizeSmall + 1
                            font.weight: Font.Medium
                            color: Theme.surfaceText
                            anchors.verticalCenter: parent.verticalCenter
                        }

                        Rectangle {
                            width: 8
                            height: 8
                            radius: 4
                            color: screen.stateColor(envRow.modelData.state)
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }

                    StyledText {
                        width: parent.width
                        wrapMode: Text.WordWrap
                        text: envRow.remote ? envRow.modelData.text + (envRow.modelData.connected && !envRow.modelData.usage ? " Cost and tokens need View diagnostics and usage." : "") : screen.localText(envRow.modelData)
                        font.pixelSize: Theme.fontSizeSmall
                        color: Theme.surfaceVariantText
                    }

                    StyledText {
                        visible: envRow.remote
                        width: parent.width
                        elide: Text.ElideMiddle
                        text: envRow.remote ? Fmt.machineName(envRow.modelData.machine) + " · " + envRow.modelData.origin : ""
                        font.pixelSize: Theme.fontSizeSmall - 2
                        color: Theme.surfaceVariantText
                        opacity: 0.8
                    }

                    StyledText {
                        visible: !envRow.remote && envRow.modelData.state !== "online"
                        width: parent.width
                        wrapMode: Text.WordWrap
                        text: "For live data, paste a pairing link from this computer's T3 Code below, or use Usage → Limits → Connect…"
                        font.pixelSize: Theme.fontSizeSmall - 2
                        color: Theme.surfaceVariantText
                        opacity: 0.8
                    }

                    Row {
                        visible: envRow.remote
                        spacing: Theme.spacingS
                        topPadding: 2

                        SmallButton {
                            visible: envRow.hasGrant
                            text: "Forget access"
                            onClicked: screen.host.sendLiveCommand({
                                "cmd": "forgetEnvironment",
                                "environment": envRow.modelData.id
                            })
                        }

                        SmallButton {
                            text: screen.confirmRemove === envRow.modelData.id ? "Confirm remove" : "Remove"
                            danger: screen.confirmRemove === envRow.modelData.id
                            onClicked: {
                                if (screen.confirmRemove !== envRow.modelData.id) {
                                    screen.confirmRemove = envRow.modelData.id;
                                    return;
                                }
                                screen.confirmRemove = "";
                                if (screen.host.selection === envRow.modelData.id)
                                    screen.host.selectEnvironment("all");
                                screen.host.sendLiveCommand({
                                    "cmd": "removeEnvironment",
                                    "environment": envRow.modelData.id
                                });
                            }
                        }
                    }
                }
            }
        }
    }

    // ----- add an environment -----

    Column {
        width: parent.width
        spacing: Theme.spacingS

        StyledText {
            text: "Add an environment"
            font.pixelSize: Theme.fontSizeSmall + 1
            font.weight: Font.Medium
            color: Theme.surfaceText
        }

        StyledText {
            width: parent.width
            wrapMode: Text.WordWrap
            text: "On the other machine's T3 Code: Settings → Connections → Authorized clients → Create link. Under Permissions keep View environment, plus View diagnostics and usage for cost and tokens, then copy the whole link. Paste a new link here to reconnect an environment that needs access."
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.surfaceVariantText
        }

        DTextField {
            id: linkField
            width: parent.width
            labelText: "Pairing link"
            placeholderText: "https://…/pair#token=…"
            echoMode: TextInput.Password
            showPasswordToggle: true
            onAccepted: {
                if (text.trim().length > 0 && !screen.pendingSeq)
                    screen.addEnvironment();
            }
        }

        Row {
            spacing: Theme.spacingS

            SmallButton {
                text: screen.pendingSeq ? "Connecting…" : "Connect"
                primary: true
                enabled: linkField.text.trim().length > 0 && !screen.pendingSeq
                onClicked: screen.addEnvironment()
            }
        }

        StyledText {
            visible: screen.result.length > 0
            width: parent.width
            wrapMode: Text.WordWrap
            text: screen.result
            font.pixelSize: Theme.fontSizeSmall
            color: screen.resultOk ? Theme.success : Theme.error
        }

        StyledText {
            width: parent.width
            wrapMode: Text.WordWrap
            text: "Read-only. HTTPS addresses work anywhere; plain HTTP only for this computer or a Tailscale peer. " + (screen.info.keyring ? "Access is kept in the keyring; the list of environments in ~/.config/t3codebar/connections.json." : "secret-tool is not installed, so access lasts until the shell restarts.")
            font.pixelSize: Theme.fontSizeSmall - 2
            color: Theme.surfaceVariantText
            opacity: 0.8
        }
    }

    component SmallButton: Rectangle {
        id: button
        property alias text: buttonLabel.text
        property bool primary: false
        property bool danger: false
        signal clicked
        width: buttonLabel.implicitWidth + Theme.spacingM * 2
        height: 28
        radius: 14
        opacity: enabled ? 1 : 0.45
        readonly property color tone: danger ? Theme.error : Theme.primary
        color: primary ? (buttonArea.containsMouse ? Qt.lighter(tone, 1.1) : tone) : (buttonArea.containsMouse ? Theme.withAlpha(tone, 0.18) : Theme.withAlpha(tone, 0.1))

        StyledText {
            id: buttonLabel
            anchors.centerIn: parent
            font.pixelSize: Theme.fontSizeSmall
            font.weight: Font.Medium
            color: button.primary ? Theme.primaryText : button.tone
        }

        MouseArea {
            id: buttonArea
            anchors.fill: parent
            hoverEnabled: true
            enabled: button.enabled
            cursorShape: Qt.PointingHandCursor
            onClicked: button.clicked()
        }

        Accessible.name: text
        Accessible.role: Accessible.Button
    }
}
