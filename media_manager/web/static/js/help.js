/** Hash helpers for the in-app Help view (`#help`, `#help/redownload`). */
window.parseAppHash = function (hash) {
    const raw = String(hash || "").replace(/^#/, "").trim() || "inventory";
    const slash = raw.indexOf("/");
    if (slash === -1) {
        return { viewId: raw, topic: null };
    }
    let topic = raw.slice(slash + 1);
    try {
        topic = decodeURIComponent(topic);
    } catch (e) {
        /* keep raw topic */
    }
    return { viewId: raw.slice(0, slash), topic: topic || null };
};

window.scrollHelpTopic = function (topic) {
    if (!topic) return;
    const el = document.getElementById("help-" + topic);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
};
