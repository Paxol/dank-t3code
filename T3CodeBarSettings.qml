import QtQuick
import qs.Common
import qs.Widgets
import qs.DCommon.Widgets
import qs.Modules.Plugins

PluginSettings {
    id: root

    pluginId: "t3codeBar"

    StyledText {
        width: parent.width
        text: "T3 Code Bar"
        font.pixelSize: Theme.fontSizeLarge
        font.weight: Font.Bold
        color: Theme.surfaceText
    }

    StyledText {
        width: parent.width
        text: "Reads T3 Code's local data read-only and never writes to its files. The only network traffic is the optional live connection to 127.0.0.1 for threads, limits and usage, set up from Usage → Limits, and the remote environments you pair from the popout's Environments button."
        font.pixelSize: Theme.fontSizeSmall
        color: Theme.surfaceVariantText
        wrapMode: Text.WordWrap
    }

    ToggleSetting {
        settingKey: "showBarLabel"
        label: "Status label in the bar"
        description: "Show Approval, Input, Failed and similar labels next to the T3 mark, or the number of running threads"
        defaultValue: true
    }

    ToggleSetting {
        settingKey: "hideWhenEmpty"
        label: "Hide when the inbox is empty"
        description: "Remove the widget from the bar while there are no active threads"
        defaultValue: false
    }

    ToggleSetting {
        settingKey: "showPreviews"
        label: "Show previews"
        description: "Rest the pointer on a thread to read the start of its last reply. Turn off while sharing your screen."
        defaultValue: true
    }

    ToggleSetting {
        settingKey: "notifications"
        label: "Notifications"
        description: "Notify when a thread needs approval or input, gets a guest request, fails, or hits a usage limit"
        defaultValue: true
    }

    ToggleSetting {
        settingKey: "t3Colors"
        label: "T3 Code status colors"
        description: "Use T3 Code's own status colors instead of the shell theme"
        defaultValue: true
    }

    ToggleSetting {
        settingKey: "clock24"
        label: "24-hour clock"
        description: "Reset times on the Limits tab"
        defaultValue: true
    }

    SelectionSetting {
        settingKey: "pollSeconds"
        label: "Refresh interval"
        description: "How often to check T3 Code's database for changes"
        options: [
            {
                "label": "0.5 seconds",
                "value": "0.5"
            },
            {
                "label": "1 second",
                "value": "1"
            },
            {
                "label": "2 seconds",
                "value": "2"
            },
            {
                "label": "5 seconds",
                "value": "5"
            }
        ]
        defaultValue: "1"
    }

    SelectionSetting {
        settingKey: "maxListHeight"
        label: "Popout height"
        description: "Longer lists scroll"
        options: [
            {
                "label": "Compact",
                "value": "360"
            },
            {
                "label": "Normal",
                "value": "520"
            },
            {
                "label": "Tall",
                "value": "720"
            }
        ]
        defaultValue: "520"
    }

    StringSetting {
        settingKey: "stateDir"
        label: "T3 Code data directory"
        description: "Folder with statev2.sqlite. Empty uses $T3CODE_HOME/userdata, else ~/.t3/userdata."
        placeholder: "~/.t3/userdata"
        defaultValue: ""
    }

    StringSetting {
        settingKey: "desktopId"
        label: "T3 Code desktop entry"
        description: "Desktop entry ID used by Open T3 Code and a right-click on the bar mark"
        placeholder: "com.t3tools.T3Code"
        defaultValue: ""
    }
}
