// Settings page (decision 6's editable extraction rules + MS account
// connect/disconnect status + list_table refactor's List summary table).
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
// List summary table + edit modal
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

// Tracks the server's last-known default_category (updated on initial
// load and on a successful Settings save) -- used to seed the select the
// very first time, without clobbering an in-progress (unsaved) selection
// on later refreshes triggered by editing/deleting a list row.
var _lastKnownDefaultCategory = null;

function renderListSummaryRow(entry) {
  var tr = document.createElement("tr");
  var category = (entry.list_category || [])[0] || "";
  tr.innerHTML =
    "<td></td><td></td><td></td><td></td>" +
    '<td class="ls-default-cell"></td>' +
    '<td style="text-align:center;"><button type="button" class="icon-btn" title="Edit"><i class="fas fa-edit"></i></button></td>';
  tr.children[0].textContent = category;
  tr.children[1].textContent = entry.list_name;
  tr.children[2].textContent = (entry.list_alt_names || []).join("; ");
  tr.children[3].textContent = (entry.list_keywords || []).join("; ");
  if (entry.list_is_category_default) {
    tr.querySelector(".ls-default-cell").innerHTML = '<i class="fas fa-check ls-default-tick"></i>';
  }
  tr.querySelector(".icon-btn").addEventListener("click", function() { openListEditModal(entry); });
  return tr;
}

async function loadListEntries() {
  var listEntries = await apiFetch("/api/list-entries");
  var body = $id("listSummaryBody");
  body.innerHTML = "";
  var sorted = listEntries.slice().sort(function(a, b) {
    var catA = (a.list_category || [])[0] || "";
    var catB = (b.list_category || [])[0] || "";
    if (catA !== catB) { return catA.localeCompare(catB); }
    return (a.list_name || "").localeCompare(b.list_name || "");
  });
  sorted.forEach(function(entry) { body.appendChild(renderListSummaryRow(entry)); });
  var categorySelect = $id("defaultCategory");
  var currentSelection = categorySelect.options.length > 0 ? categorySelect.value : _lastKnownDefaultCategory;
  populateCategorySelect(currentSelection, listEntries);
  return listEntries;
}

// ---------------------------------------------------------------------------
// List edit/add modal
// ---------------------------------------------------------------------------

var listEditModal = $id("listEditModal");
var _editingListId = null; // null while the modal is in "add new" mode

function openListEditModal(entry) {
  entry = entry || { list_id: null };
  _editingListId = entry.list_id;
  $id("listEditModalTitle").innerHTML = entry.list_id
    ? '<i class="fas fa-list"></i> Edit list' : '<i class="fas fa-list"></i> Add list';
  $id("leName").value = entry.list_name || "";
  $id("leAlt").value = listToLines(entry.list_alt_names);
  $id("leCategory").value = (entry.list_category || [])[0] || "";
  $id("leKeywords").value = listToLines(entry.list_keywords);
  $id("leDefault").checked = !!entry.list_is_category_default;
  $id("leDeleteBtn").style.display = entry.list_id ? "" : "none";
  listEditModal.classList.add("open");
}

function closeListEditModal() { listEditModal.classList.remove("open"); }

listEditModal.addEventListener("click", function(e) { if (e.target === listEditModal) { closeListEditModal(); } });
$id("leCancelBtn").addEventListener("click", closeListEditModal);

// A <input list=...> datalist filters its suggestions by the field's
// current text -- with an existing list's full name already filled in,
// that leaves only itself as a match. Selecting the text on focus means
// the very next keystroke replaces it, so the full set of real MS list
// names shows up immediately instead of needing a manual clear first.
$id("leName").addEventListener("focus", function() { this.select(); });

$id("addListEntryBtn").addEventListener("click", function() { openListEditModal(null); });

$id("leSaveBtn").addEventListener("click", function() {
  var payload = {
    list_name: $id("leName").value.trim(),
    list_alt_names: linesToList($id("leAlt").value),
    list_category: $id("leCategory").value.trim() ? [$id("leCategory").value.trim()] : [],
    list_keywords: linesToList($id("leKeywords").value),
    list_is_category_default: $id("leDefault").checked,
  };
  if (!payload.list_name) {
    showToast("List name is required", "error");
    return;
  }
  var request = _editingListId
    ? apiFetch("/api/list-entries/" + _editingListId, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      })
    : apiFetch("/api/list-entries", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
  request
    .then(function() {
      showToast(_editingListId ? "List saved" : "List created", "success");
      closeListEditModal();
      return loadListEntries();
    })
    .catch(function(err) { showToast("Could not save list: " + err.message, "error"); });
});

$id("leDeleteBtn").addEventListener("click", function() {
  if (!_editingListId) { return; }
  if (!window.confirm('Delete list "' + $id("leName").value + '"? This can\'t be undone.')) { return; }
  apiFetch("/api/list-entries/" + _editingListId, { method: "DELETE" })
    .then(function() {
      showToast("List removed", "success");
      closeListEditModal();
      return loadListEntries();
    })
    .catch(function(err) { showToast("Could not delete list: " + err.message, "error"); });
});

// ---------------------------------------------------------------------------
// Load
// ---------------------------------------------------------------------------

async function loadSettings() {
  var settings = await apiFetch("/api/settings");
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
// Save (default_timezone / default_category / extraction_custom_instructions
// only -- list entries save independently via the list-edit modal's own
// Save Changes/Delete List buttons; list_override_rules is no longer
// user-editable, see the "Extraction prompt" card's custom instructions)
// ---------------------------------------------------------------------------

$id("saveBtn").addEventListener("click", function() {
  var payload = {
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
