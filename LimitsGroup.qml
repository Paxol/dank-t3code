import QtQuick
import qs.Common
import qs.DCommon.Widgets

// One provider on the Limits tab: its accounts, then a card per window pool
// with the share left, the pace, and a bar of what is left.
Column {
    id: providerGroup

    property var group: ({})

    spacing: Theme.spacingS

    function paceText(pace) {
        switch (pace) {
        case "ahead":
            return "Ahead of pace";
        case "under":
            return "Under pace";
        case "on":
            return "On pace";
        }
        return "";
    }

    function paceColor(pace) {
        return pace === "ahead" ? Theme.warning : Theme.surfaceVariantText;
    }

    function levelColor(left) {
        if (left <= 10)
            return Theme.error;
        if (left <= 25)
            return Theme.warning;
        return Theme.primary;
    }

    Row {
        spacing: Theme.spacingS

        Image {
            visible: !!providerGroup.group.icon
            width: 18
            height: 18
            source: providerGroup.group.icon ? Qt.resolvedUrl("assets/providers/" + providerGroup.group.icon + (providerGroup.group.icon === "antigravity" ? ".png" : ".svg")) : ""
            sourceSize: Qt.size(36, 36)
            fillMode: Image.PreserveAspectFit
            anchors.verticalCenter: parent.verticalCenter
        }

        StyledText {
            text: providerGroup.group.label || ""
            font.pixelSize: Theme.fontSizeMedium
            font.weight: Font.Medium
            color: Theme.surfaceText
            anchors.verticalCenter: parent.verticalCenter
        }
    }

    Repeater {
        model: providerGroup.group.accounts || []

        delegate: StyledText {
            required property var modelData
            width: providerGroup.width
            text: modelData.line
            font.pixelSize: Theme.fontSizeSmall - 1
            color: Theme.surfaceVariantText
            elide: Text.ElideRight
        }
    }

    Repeater {
        model: providerGroup.group.rows || []

        delegate: Rectangle {
            id: card
            required property var modelData
            width: providerGroup.width
            height: cardColumn.implicitHeight + Theme.spacingM * 2
            radius: Theme.cornerRadius
            color: Theme.withAlpha(Theme.surfaceText, 0.05)

            Column {
                id: cardColumn
                x: Theme.spacingM
                y: Theme.spacingM
                width: parent.width - Theme.spacingM * 2
                spacing: Theme.spacingS

                Item {
                    width: parent.width
                    height: leftText.implicitHeight

                    StyledText {
                        anchors.left: parent.left
                        anchors.right: leftText.left
                        anchors.rightMargin: Theme.spacingS
                        anchors.verticalCenter: parent.verticalCenter
                        text: card.modelData.label
                        font.pixelSize: Theme.fontSizeSmall + 1
                        font.weight: Font.Medium
                        color: Theme.surfaceText
                        elide: Text.ElideRight
                    }

                    StyledText {
                        id: leftText
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        text: card.modelData.remaining + "% left"
                        font.pixelSize: Theme.fontSizeSmall + 1
                        font.weight: Font.Medium
                        color: providerGroup.levelColor(card.modelData.remaining)
                    }
                }

                StyledText {
                    visible: !!card.modelData.pace
                    text: providerGroup.paceText(card.modelData.pace)
                    font.pixelSize: Theme.fontSizeSmall - 1
                    color: providerGroup.paceColor(card.modelData.pace)
                }

                Repeater {
                    model: card.modelData.bars

                    delegate: Column {
                        id: barBlock
                        required property var modelData
                        width: cardColumn.width
                        spacing: 4

                        StyledText {
                            visible: card.modelData.bars.length > 1
                            text: barBlock.modelData.account + " · " + barBlock.modelData.left + "% left"
                            font.pixelSize: Theme.fontSizeSmall - 1
                            color: Theme.surfaceVariantText
                        }

                        // What is left, with even pace marked and what the reset restores hatched in.
                        Item {
                            width: parent.width
                            height: 8

                            Rectangle {
                                anchors.fill: parent
                                radius: 4
                                color: Theme.withAlpha(Theme.surfaceText, 0.1)
                            }

                            Rectangle {
                                id: leftFill
                                width: parent.width * barBlock.modelData.left / 100
                                height: parent.height
                                radius: 4
                                color: providerGroup.levelColor(barBlock.modelData.left)
                            }

                            Rectangle {
                                visible: barBlock.modelData.restores > 0
                                x: leftFill.width
                                width: Math.min(parent.width - x, parent.width * barBlock.modelData.restores / 100)
                                height: parent.height
                                radius: 4
                                color: Theme.withAlpha(providerGroup.levelColor(barBlock.modelData.left), 0.3)
                            }

                            Rectangle {
                                visible: barBlock.modelData.evenPaceLeft !== null && barBlock.modelData.evenPaceLeft !== undefined
                                x: Math.max(0, Math.min(parent.width - 2, parent.width * (barBlock.modelData.evenPaceLeft || 0) / 100 - 1))
                                y: -2
                                width: 2
                                height: parent.height + 4
                                radius: 1
                                color: Theme.surfaceText
                                opacity: 0.8
                            }
                        }

                        StyledText {
                            width: parent.width
                            wrapMode: Text.WordWrap
                            font.pixelSize: Theme.fontSizeSmall - 1
                            color: Theme.surfaceVariantText
                            text: {
                                const parts = [];
                                const b = barBlock.modelData;
                                if (b.resets)
                                    parts.push("Resets " + b.resets);
                                if (b.evenPaceLeft !== null && b.evenPaceLeft !== undefined)
                                    parts.push(b.evenPaceLeft + "% left by now");
                                if (b.restores > 0)
                                    parts.push("+" + b.restores + "% of pool");
                                if (b.credits)
                                    parts.push(b.credits);
                                return parts.join(" · ");
                            }
                        }
                    }
                }
            }
        }
    }
}
