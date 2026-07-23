// Settings page (decision 6's editable extraction rules + MS account
// connect/disconnect status + list_table refactor's Manage Lists panel).
// Same apiFetch pattern as the wizard's app.js -- not shared as a common
// module since neither file is large enough yet to justify the
// indirection.

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
// List <-> textarea helpers (list_override_rules / list_alt_names /
// list_category / list_keywords are JSON string arrays server-side;
// edited here as one-per-line text)
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
// Default category select -- populated from the distinct category tags
// actually used across list_table, so this can't drift from what's
// configured (typo-proof, unlike a free-text field).
// ---------------------------------------------------------------------------

function populateCategorySelect(selected, listEntries) {
  var select = $id("defaultCategory");
  var categories = new Set();
  (listEntries || []).forEach(function(entry) {
    (entry.list_category || []).forEach(function(c) { categories.add(c); });
  });
  if (selected) { categories.add(selected); }
  var sorted = Array.from(categories).sort();
  select.innerHTML = "";
  if (sorted.length === 0) {
    var opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(no categories configured yet)";
    select.appendChild(opt);
    return;
  }
  sorted.forEach(function(cat) {
    var opt = document.createElement("option");
    opt.value = cat;
    opt.textContent = cat;
    if (cat === selected) { opt.selected = true; }
    select.appendChild(opt);
  });
}

// ---------------------------------------------------------------------------
// Manage lists panel
// ---------------------------------------------------------------------------

var _msListNames = [];

function populateMsListsDatalist(msLists) {
  var datalist = $id("msListsDatalist");
  datalist.innerHTML = "";
  msLists.forEach(function(lst) {
    var opt = document.createElement("option");
    opt.value = lst.displayName;
    datalist.appendChild(opt);
  });
}

function createListEntryRow(entry) {
  // entry.list_id is null for a not-yet-created blank row.
  var row = document.createElement("div");
  row.className = "list-entry-row";
  row.innerHTML =
    '<div class="field">' +
      '<label>List name</label>' +
      '<input type="text" class="le-name" list="msListsDatalist" placeholder="e.g. Household Tasks">' +
    '</div>' +
    '<div class="field">' +
      '<label>Alt names <span style="font-weight:400;">(one per line, optional)</span></label>' +
      '<textarea class="le-alt" rows="2" placeholder="[Example 1]&#10;[Example 2]"></textarea>' +
    '</div>' +
    '<div class="field">' +
      '<label>Category</label>' +
      '<input type="text" class="le-category" placeholder="e.g. Theo">' +
    '</div>' +
    '<div class="field">' +
      '<label>Keywords <span style="font-weight:400;">(one per line, optional)</span></label>' +
      '<textarea class="le-keywords" rows="3" placeholder="[Example 1]&#10;[Example 2]"></textarea>' +
    '</div>' +
    '<label style="display:flex; align-items:center; gap:8px; font-size:14px; margin-bottom:12px;">' +
      '<input type="checkbox" class="le-default" style="width:16px; height:16px;"> Default list for this category' +
    '</label>' +
    '<div class="list-entry-actions">' +
      '<button type="button" class="btn btn-primary btn-sm le-save">' +
        (entry.list_id ? "Save" : "Create") +
      '</button>' +
      (entry.list_id ? '<button type="button" class="btn btn-outline btn-sm le-delete">Delete</button>' : '') +
    '</div>';

  row.querySelector(".le-name").value = entry.list_name || "";
  row.querySelector(".le-alt").value = listToLines(entry.list_alt_names);
  row.querySelector(".le-category").value = (entry.list_category || [])[0] || "";
  row.querySelector(".le-keywords").value = listToLines(entry.list_keywords);
  row.querySelector(".le-default").checked = !!entry.list_is_category_default;

  row.querySelector(".le-save").addEventListener("click", function() {
    var payload = {
      list_name: row.querySelector(".le-name").value.trim(),
      list_alt_names: linesToList(row.querySelector(".le-alt").value),
      list_category: row.querySelector(".le-category").value.trim() ? [row.querySelector(".le-category").value.trim()] : [],
      list_keywords: linesToList(row.querySelector(".le-keywords").value),
      list_is_category_default: row.querySelector(".le-default").checked,
    };
    if (!payload.list_name) {
      showToast("List name is required", "error");
      return;
    }
    var request = entry.list_id
      ? apiFetch("/api/list-entries/" + entry.list_id, {
          method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
        })
      : apiFetch("/api/list-entries", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
        });
    request
      .then(function() {
        showToast(entry.list_id ? "List saved" : "List created", "success");
        return loadListEntries();
      })
      .catch(function(err) { showToast("Could not save list: " + err.message, "error"); });
  });

  var deleteBtn = row.querySelector(".le-delete");
  if (deleteBtn) {
    deleteBtn.addEventListener("click", function() {
      apiFetch("/api/list-entries/" + entry.list_id, { method: "DELETE" })
        .then(function() {
          showToast("List removed", "success");
          return loadListEntries();
        })
        .catch(function(err) { showToast("Could not delete list: " + err.message, "error"); });
    });
  }

  return row;
}

var _pendingNewRow = false;
// Tracks the server's last-known default_category (updated on initial
// load and on a successful Settings save) -- used to seed the select the
// very first time, without clobbering an in-progress (unsaved) selection
// on later refreshes triggered by editing/deleting a list row.
var _lastKnownDefaultCategory = null;

async function loadListEntries() {
  var listEntries = await apiFetch("/api/list-entries");
  var container = $id("listEntriesContainer");
  container.innerHTML = "";
  listEntries.forEach(function(entry) { container.appendChild(createListEntryRow(entry)); });
  if (_pendingNewRow) {
    container.appendChild(createListEntryRow({ list_id: null }));
  }
  var categorySelect = $id("defaultCategory");
  var currentSelection = categorySelect.options.length > 0 ? categorySelect.value : _lastKnownDefaultCategory;
  populateCategorySelect(currentSelection, listEntries);
  return listEntries;
}

$id("addListEntryBtn").addEventListener("click", function() {
  _pendingNewRow = true;
  $id("listEntriesContainer").appendChild(createListEntryRow({ list_id: null }));
});

// ---------------------------------------------------------------------------
// Load
// ---------------------------------------------------------------------------

async function loadSettings() {
  var settings = await apiFetch("/api/settings");
  $id("listOverrideRules").value = listToLines(settings.list_override_rules);
  $id("extractionCustomInstructions").value = settings.extraction_custom_instructions || "";
  populateTimezoneSelect(settings.default_timezone);
  _lastKnownDefaultCategory = settings.default_category || null;
  return settings;
}

// Read-only, always-accurate render of the actual prompt(s) extract_tasks
// would send right now -- backed by the same poe_client.build_prompt_previews()
// the real extraction call uses, so it can't drift from reality.
async function loadPromptPreview() {
  var preview = await apiFetch("/api/settings/prompt-preview");
  $id("promptPreviewImage").value = preview.image_prompt;
  $id("promptPreviewText").value = preview.text_prompt;
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
// Save (list_override_rules / default_timezone / default_category only --
// Manage Lists rows save independently via their own Save/Create buttons)
// ---------------------------------------------------------------------------

$id("saveBtn").addEventListener("click", function() {
  var payload = {
    list_override_rules: linesToList($id("listOverrideRules").value),
    default_timezone: $id("defaultTimezone").value,
    default_category: $id("defaultCategory").value,
    extraction_custom_instructions: $id("extractionCustomInstructions").value,
  };
  apiFetch("/api/settings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then(function() {
      _lastKnownDefaultCategory = payload.default_category;
      showToast("Settings saved", "success");
      return loadPromptPreview();
    })
    .catch(function(err) { showToast("Could not save settings: " + err.message, "error"); });
});

(async function init() {
  try {
    await loadSettings();
    var msLists = await apiFetch("/api/lists");
    populateMsListsDatalist(msLists);
    await loadListEntries();
    await loadPromptPreview();
  } catch (err) {
    showToast("Could not load settings: " + err.message, "error");
  }
  try {
    await loadMsStatus();
  } catch (err) {
    showToast("Could not load Microsoft account status: " + err.message, "error");
  }
})();
