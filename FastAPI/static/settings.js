// Settings page (decision 6's editable extraction rules + MS account
// connect/disconnect status). Same apiFetch pattern as the wizard's
// app.js -- not shared as a common module since neither file is large
// enough yet to justify the indirection.

function $id(id) { return document.getElementById(id); }

async function apiFetch(url, options) {
  options = options || {};
  var resp = await fetch(url, options);
  var data = null;
  try { data = await resp.json(); } catch (ignored) { /* no JSON body */ }
  if (!resp.ok) {
    var detail = (data && data.detail)
      ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))
      : ("HTTP " + resp.status);
    var err = new Error(detail);
    err.status = resp.status;
    throw err;
  }
  return data;
}

function showToast(msg, type) {
  type = type || "info";
  var tc = $id("toastContainer");
  var t = document.createElement("div");
  t.className = "toast " + type;
  t.textContent = msg;
  tc.appendChild(t);
  setTimeout(function() {
    t.style.opacity = "0";
    t.style.transition = "opacity 0.3s";
    setTimeout(function() { t.remove(); }, 300);
  }, 4000);
}

// ---------------------------------------------------------------------------
// List <-> textarea helpers (priority_keywords / list_override_rules are
// JSON string arrays server-side; edited here as one-per-line text)
// ---------------------------------------------------------------------------

function linesToList(text) {
  return text.split("\n").map(function(s) { return s.trim(); }).filter(function(s) { return s.length > 0; });
}

function listToLines(list) {
  return (list || []).join("\n");
}

// ---------------------------------------------------------------------------
// Timezone select -- same reconstructed list as the wizard's app.js
// ---------------------------------------------------------------------------

function populateTimezoneSelect(selected) {
  var select = $id("defaultTimezone");
  var common = ["Asia/Hong_Kong", "Asia/Tokyo", "Asia/Shanghai", "Asia/Singapore",
    "Europe/London", "America/New_York", "America/Los_Angeles", "UTC"];
  if (selected && common.indexOf(selected) === -1) { common.unshift(selected); }
  select.innerHTML = "";
  common.forEach(function(tz) {
    var opt = document.createElement("option");
    opt.value = tz;
    opt.textContent = tz;
    if (tz === selected) { opt.selected = true; }
    select.appendChild(opt);
  });
}

// ---------------------------------------------------------------------------
// Load
// ---------------------------------------------------------------------------

async function loadSettings() {
  var settings = await apiFetch("/api/settings");
  $id("priorityKeywords").value = listToLines(settings.priority_keywords);
  $id("listOverrideRules").value = listToLines(settings.list_override_rules);
  populateTimezoneSelect(settings.default_timezone);
  $id("defaultListPriority").value = settings.default_list_name_priority || "";
  $id("defaultListOther").value = settings.default_list_name_other || "";
  $id("defaultListEvent").value = settings.default_list_name_event || "";
  var lite = settings.lite_mode_list_names || {};
  $id("liteListPriority").value = lite.priority || "";
  $id("liteListOther").value = lite.other || "";
  $id("liteListEvent").value = lite.event || "";
}

async function loadMsStatus() {
  var config = await apiFetch("/api/config");
  if (config.ms_linked) {
    $id("msAccountName").textContent = config.ms_account_name || "(connected)";
    $id("msConnected").style.display = "";
    $id("msNotConnected").style.display = "none";
  } else {
    $id("msConnected").style.display = "none";
    $id("msNotConnected").style.display = "";
  }
}

// ---------------------------------------------------------------------------
// Save
// ---------------------------------------------------------------------------

$id("saveBtn").addEventListener("click", function() {
  var payload = {
    priority_keywords: linesToList($id("priorityKeywords").value),
    list_override_rules: linesToList($id("listOverrideRules").value),
    default_timezone: $id("defaultTimezone").value,
    default_list_name_priority: $id("defaultListPriority").value.trim() || null,
    default_list_name_other: $id("defaultListOther").value.trim() || null,
    default_list_name_event: $id("defaultListEvent").value.trim() || null,
    lite_mode_list_names: {
      priority: $id("liteListPriority").value.trim(),
      other: $id("liteListOther").value.trim(),
      event: $id("liteListEvent").value.trim(),
    },
  };
  apiFetch("/api/settings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then(function() { showToast("Settings saved", "success"); })
    .catch(function(err) { showToast("Could not save settings: " + err.message, "error"); });
});

(async function init() {
  try {
    await loadSettings();
  } catch (err) {
    showToast("Could not load settings: " + err.message, "error");
  }
  try {
    await loadMsStatus();
  } catch (err) {
    showToast("Could not load Microsoft account status: " + err.message, "error");
  }
})();
