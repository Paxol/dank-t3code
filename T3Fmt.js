.pragma library

// Text formats of T3 Code's sidebar and Usage page, as the original T3 Code Bar writes them.

// "now", "5m", "3h", "2d": the idle time of a thread.
function relative(ms, now) {
    if (!ms)
        return "";
    const seconds = Math.max(0, Math.floor((now - ms) / 1000));
    if (seconds < 60)
        return "now";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60)
        return minutes + "m";
    const hours = Math.floor(minutes / 60);
    if (hours < 24)
        return hours + "h";
    return Math.floor(hours / 24) + "d";
}

// "45s", "12m", "1h 5m": how long a run has been going.
function elapsed(ms, now) {
    if (!ms)
        return "";
    const seconds = Math.max(0, Math.floor((now - ms) / 1000));
    if (seconds < 60)
        return seconds + "s";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60)
        return minutes + "m";
    return Math.floor(minutes / 60) + "h " + (minutes % 60) + "m";
}

// "45m", "2h", "3d" until a future time, or "" once it has passed.
function until(ms, now) {
    if (!ms)
        return "";
    const seconds = Math.ceil((ms - now) / 1000);
    if (seconds <= 0)
        return "";
    const minutes = Math.max(1, Math.floor((seconds + 59) / 60));
    if (minutes < 60)
        return minutes + "m";
    const hours = Math.floor((minutes + 59) / 60);
    if (hours < 24)
        return hours + "h";
    return Math.floor((hours + 23) / 24) + "d";
}

function ago(ms, now) {
    const text = relative(ms, now);
    return text === "now" ? "just now" : text + " ago";
}

function grouped(text) {
    const negative = text.charAt(0) === "-";
    const unsigned = negative ? text.slice(1) : text;
    const dot = unsigned.indexOf(".");
    let whole = dot >= 0 ? unsigned.slice(0, dot) : unsigned;
    const rest = dot >= 0 ? unsigned.slice(dot) : "";
    whole = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (negative ? "-" : "") + whole + rest;
}

// "16B", "1.50M", "193M", "4.2K", "999".
function tokens(value) {
    const units = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
    const magnitude = Math.abs(value);
    for (const unit of units) {
        if (magnitude >= unit[0]) {
            const scaled = value / unit[0];
            const places = Math.abs(scaled) >= 100 ? 0 : Math.abs(scaled) >= 10 ? 1 : 2;
            let text = scaled.toFixed(places);
            if (text.indexOf(".") >= 0 && /^\.0+$/.test(text.slice(text.indexOf("."))))
                text = text.slice(0, text.indexOf("."));
            return text + unit[1];
        }
    }
    return grouped(String(Math.round(value)));
}

// "$1,234.57".
function currency(value) {
    const text = Math.abs(value).toFixed(2);
    return (value < 0 ? "-$" : "$") + grouped(text);
}

// A share between 0 and 1 as "12.3%", with "<0.1%" for tiny shares.
function percent(share) {
    const p = share * 100;
    if (p > 0 && p < 0.1)
        return "<0.1%";
    return p.toFixed(1) + "%";
}

function sessions(count) {
    return grouped(String(count)) + (count === 1 ? " session" : " sessions");
}

// Column caption on the usage chart.
function columnLabel(ms, hourly) {
    const date = new Date(ms);
    if (hourly) {
        const end = new Date(ms + 3600000);
        const pad = n => (n < 10 ? "0" : "") + n;
        return pad(date.getHours()) + ":00–" + pad(end.getHours()) + ":00";
    }
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return months[date.getMonth()] + " " + date.getDate();
}

// ThreadSummary across environments: the strongest status and the badge count the bar shows.
// The same rules as `summarize` in backend/t3bar.py, which summarizes one environment.
const STATUS_PRIORITY = {
    "approval": 8, "input": 7, "request": 7, "goal": 6, "working": 5, "failed": 4, "limited": 4,
    "woke": 3, "done": 1, "settled": 0, "waiting": 0
};
const STATUS_LABELS = {
    "working": "Working", "goal": "Goal", "approval": "Approval", "input": "Input", "request": "Request",
    "woke": "Woke", "failed": "Failed", "limited": "Limited", "waiting": "Waiting", "done": "Done",
    "settled": "Settled"
};
const ATTENTION = ["working", "goal", "approval", "input", "request", "failed", "limited", "woke"];
const RUNNING = ["working", "goal"];
const LABEL_IN_BAR = ["approval", "input", "request", "woke", "failed", "limited"];

function priority(status) {
    return STATUS_PRIORITY[status] !== undefined ? STATUS_PRIORITY[status] : 0;
}

// `threads` are the rows whose source is live; others count as settled.
function summarize(threads) {
    let status = "settled", attention = 0, running = 0;
    for (const thread of threads) {
        if (priority(thread.status) > priority(status))
            status = thread.status;
        if (ATTENTION.indexOf(thread.status) >= 0)
            attention += 1;
        if (RUNNING.indexOf(thread.status) >= 0)
            running += 1;
    }
    let badge = 0;
    if (RUNNING.indexOf(status) >= 0)
        badge = running;
    else if (ATTENTION.indexOf(status) >= 0)
        badge = threads.filter(t => priority(t.status) === priority(status)).length;
    return {
        "status": status,
        "label": STATUS_LABELS[status] || status,
        "showsLabel": LABEL_IN_BAR.indexOf(status) >= 0,
        "attention": attention,
        "running": running,
        "badge": badge
    };
}

// Material Symbols for T3 Code's machine kinds (environmentIcon / platform.machine).
function machineIcon(kind) {
    switch (kind) {
    case "cloud":
        return "cloud";
    case "linux":
        return "terminal";
    case "desktop":
        return "desktop_windows";
    case "laptop":
        return "laptop";
    case "mac-mini":
    case "mac-studio":
        return "desktop_mac";
    }
    return "dns";
}

function machineName(kind) {
    switch (kind) {
    case "cloud":
        return "Cloud VM";
    case "linux":
        return "Linux/WSL";
    case "desktop":
        return "Desktop";
    case "laptop":
        return "Laptop";
    case "mac-mini":
        return "Mini PC";
    case "mac-studio":
        return "Workstation";
    }
    return "Server";
}
