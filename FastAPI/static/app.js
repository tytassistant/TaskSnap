// TaskSnap wizard UI. Ported from the current tasksnap/index.html's inline
// <script> (plan §7) -- same rendering/interaction code where it's
// architecture-independent (image upload/paste, lightbox, task-card
// rendering, list modal building). Every place that used to call MSAL,
// Poe, or MS Graph directly now calls this app's own /api/* endpoints
// instead (decision 8's draft model) -- extraction, list lookups, and sync
// are all server-side now.
//
// Deliberate simplifications from the original (noted where they occur):
// - Photo date is shown read-only (informational), not editable -- editing
//   it used to recompute any task still at the "default" due date
//   client-side; that recompute logic has no server-side equivalent yet
//   (photo_date isn't persisted on the draft), so full editability was
//   out of scope for this pass.
// - Sync progress is a single indeterminate bar, not per-task "task N of
//   M" text -- the whole sync now happens in one server-side call
//   (POST /api/drafts/{id}/sync) instead of a client-side loop hitting
//   Graph directly per task.
// - Timezone select is a small reconstructed list (Intl-detected zone +
//   a few common ones) -- the original's exact population logic wasn't
//   captured during porting.

function $id(id) { return document.getElementById(id); }

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

var state = {
  currentStep: 1,
  draftId: null,
  draftStatus: null,
  uploadedFile: null,
  photoDataUrl: null,
  photoDate: null,
  tasks: [],
  msLinked: false,
};

var lastUserInstruction = "";
var lastInputHadText = false;
var smartPasteDataUrl = null;

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------

var uploadZone = $id("uploadZone");
var imageInput = $id("imageInput");
var uploadPreview = $id("uploadPreview");
var previewImg = $id("previewImg");
var removeImgBtn = $id("removeImgBtn");
var cameraInput = $id("cameraInput");
var cameraBtn = $id("cameraBtn");
var cameraRow = $id("cameraRow");
var smartTextInput = $id("smartTextInput");
var smartPastePreview = $id("smartPastePreview");
var smartPasteImg = $id("smartPasteImg");
var smartPasteRemove = $id("smartPasteRemove");
var timezoneSelect = $id("timezoneSelect");
var extractBtn = $id("extractBtn");
var liteModeCheck = $id("liteModeCheck");
var msLinkNotice = $id("msLinkNotice");

var photoThumbRow = $id("photoThumbRow");
var photoThumbImg = $id("photoThumbImg");
var extractionLoading = $id("extractionLoading");
var photoDateRow = $id("photoDateRow");
var photoDateValue = $id("photoDateValue");
var photoDateInfo = $id("photoDateInfo");
var taskList = $id("taskList");
var addTaskBtn = $id("addTaskBtn");
var reviewActions = $id("reviewActions");
var backToUploadBtn = $id("backToUploadBtn");
var syncBtn = $id("syncBtn");

var syncingCard = $id("syncingCard");
var syncDoneCard = $id("syncDoneCard");
var syncStatusText = $id("syncStatusText");
var syncProgressBar = $id("syncProgressBar");
var syncDoneIcon = $id("syncDoneIcon");
var syncDoneTitle = $id("syncDoneTitle");
var syncDoneText = $id("syncDoneText");
var syncErrorDetail = $id("syncErrorDetail");
var reAuthBtn = $id("reAuthBtn");
var retryFailedBtn = $id("retryFailedBtn");
var newScanBtn = $id("newScanBtn");

var listModal = $id("listModal");
var listModalCancel = $id("listModalCancel");
var listModalConfirm = $id("listModalConfirm");

var lightboxOverlay = $id("lightboxOverlay");
var lightboxClose = $id("lightboxClose");
var lightboxWrap = $id("lightboxWrap");
var lightboxImg = $id("lightboxImg");

// ========== DARK MODE (disabled for now -- commented out, not deleted) ==========
// if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
//   document.documentElement.classList.add("dark");
// }
// window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function(e) {
//   if (e.matches) { document.documentElement.classList.add("dark"); }
//   else { document.documentElement.classList.remove("dark"); }
// });

// ========== TOAST ==========
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

// ========== STEPPER ========== (3 steps now: Input/Review/Sync)
function updateStepper(step) {
  state.currentStep = step;
  var items = document.querySelectorAll(".step-item");
  var connectors = document.querySelectorAll(".step-connector");
  items.forEach(function(el, i) {
    var s = i + 1;
    el.classList.remove("active", "completed");
    if (s < step) { el.classList.add("completed"); }
    else if (s === step) { el.classList.add("active"); }
  });
  connectors.forEach(function(el, i) {
    el.classList.toggle("done", i + 1 < step);
  });
}

function showSection(name) {
  document.querySelectorAll(".section").forEach(function(el) {
    el.classList.remove("visible");
  });
  $id("section-" + name).classList.add("visible");
}

// ---------------------------------------------------------------------------
// API helper
// ---------------------------------------------------------------------------

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
    err.isAuthError = resp.status === 401;
    throw err;
  }
  return data;
}

function apiJson(method, url, body) {
  return apiFetch(url, {
    method: method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

// ---------------------------------------------------------------------------
// Draft <-> internal task shape mapping. listName is the list the server
// already resolved this task to (list_table refactor) -- null means
// unmatched, needs a pick in the list modal before sync.
// ---------------------------------------------------------------------------

function taskFromApi(t) {
  return {
    id: t.task_id,
    kind: t.task_kind,
    isEvent: t.task_kind === "event",
    title: t.task_title || "",
    body: t.task_body || "",
    dueDateTime: t.task_due_datetime,
    timezone: t.task_timezone,
    reminderDateTime: t.task_reminder_datetime,
    // The list the server already resolved this task to (AI routing +
    // list_matcher's deterministic fallback) -- null means genuinely
    // unmatched, needs a manual pick in the list modal before sync.
    listName: t.task_list_name,
    checked: !!t.task_checked,
    synced: !!t.task_synced,
    syncedTaskId: t.task_synced_task_id,
  };
}

function applyDraftResponse(draft) {
  state.draftId = draft.draft_id;
  state.draftStatus = draft.draft_status;
  state.tasks = (draft.tasks || []).map(taskFromApi);
  if (draft.photo_date !== undefined) { state.photoDate = draft.photo_date; }
}

// ---------------------------------------------------------------------------
// Date helper (display-only port of getNextBusinessDay, for the read-only
// photo-date info text)
// ---------------------------------------------------------------------------

function getNextBusinessDay(dateStr) {
  var parts = dateStr.split("-");
  var d = new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10));
  d.setDate(d.getDate() + 1);
  var day = d.getDay();
  if (day === 6) { d.setDate(d.getDate() + 2); }
  else if (day === 0) { d.setDate(d.getDate() + 1); }
  var yyyy = d.getFullYear();
  var mm = String(d.getMonth() + 1).padStart(2, "0");
  var dd = String(d.getDate()).padStart(2, "0");
  return yyyy + "-" + mm + "-" + dd;
}

// ========== IMAGE UPLOAD ==========
uploadZone.addEventListener("click", function() { imageInput.click(); });

uploadZone.addEventListener("dragover", function(e) {
  e.preventDefault();
  uploadZone.classList.add("dragover");
});
uploadZone.addEventListener("dragleave", function() {
  uploadZone.classList.remove("dragover");
});
uploadZone.addEventListener("drop", function(e) {
  e.preventDefault();
  uploadZone.classList.remove("dragover");
  if (e.dataTransfer.files && e.dataTransfer.files[0]) {
    handleImageFile(e.dataTransfer.files[0]);
  }
});

imageInput.addEventListener("change", function() {
  if (imageInput.files && imageInput.files[0]) { handleImageFile(imageInput.files[0]); }
});

cameraBtn.addEventListener("click", function() { cameraInput.click(); });
cameraInput.addEventListener("change", function() {
  if (cameraInput.files && cameraInput.files[0]) { handleImageFile(cameraInput.files[0]); }
});

function handleImageFile(file) {
  if (!file.type.startsWith("image/")) {
    showToast("Please upload an image file", "error");
    return;
  }
  if (file.size > 20 * 1024 * 1024) {
    showToast("File too large. Max 20 MB", "error");
    return;
  }
  state.uploadedFile = file;
  var reader = new FileReader();
  reader.onload = function(e) {
    state.photoDataUrl = e.target.result;
    previewImg.src = e.target.result;
    uploadPreview.style.display = "block";
    uploadZone.style.display = "none";
    cameraRow.style.display = "none";
    updateExtractBtnState();
  };
  reader.readAsDataURL(file);
}

removeImgBtn.addEventListener("click", function() {
  state.uploadedFile = null;
  state.photoDataUrl = null;
  previewImg.src = "";
  uploadPreview.style.display = "none";
  uploadZone.style.display = "";
  cameraRow.style.display = "";
  imageInput.value = "";
  cameraInput.value = "";
  updateExtractBtnState();
});

// ========== SMART INPUT (text + paste image) ==========
function updateExtractBtnState() {
  var hasPhoto = !!state.uploadedFile;
  var hasPaste = !!smartPasteDataUrl;
  var hasText = smartTextInput.value.trim().length > 0;
  extractBtn.disabled = !(hasPhoto || hasPaste || hasText);
}

smartTextInput.addEventListener("input", updateExtractBtnState);

smartTextInput.addEventListener("paste", function(e) {
  var items = e.clipboardData && e.clipboardData.items;
  if (!items) { return; }
  for (var i = 0; i < items.length; i++) {
    if (items[i].type.indexOf("image") !== -1) {
      e.preventDefault();
      var file = items[i].getAsFile();
      if (!file) { continue; }
      var reader = new FileReader();
      reader.onload = function(ev) {
        smartPasteDataUrl = ev.target.result;
        smartPasteImg.src = ev.target.result;
        smartPastePreview.style.display = "";
        if (!state.uploadedFile) {
          state.uploadedFile = file;
          state.photoDataUrl = ev.target.result;
          previewImg.src = ev.target.result;
          uploadPreview.style.display = "block";
          uploadZone.style.display = "none";
        }
        updateExtractBtnState();
      };
      reader.readAsDataURL(file);
      break;
    }
  }
});

smartPasteRemove.addEventListener("click", function() {
  if (smartPasteDataUrl && state.photoDataUrl === smartPasteDataUrl) {
    state.uploadedFile = null;
    state.photoDataUrl = null;
    previewImg.src = "";
    uploadPreview.style.display = "none";
    uploadZone.style.display = "";
    imageInput.value = "";
  }
  smartPasteDataUrl = null;
  smartPasteImg.src = "";
  smartPastePreview.style.display = "none";
  updateExtractBtnState();
});

// ========== LIGHTBOX ==========
var lbZoom = 1;

function applyZoom() {
  lightboxImg.style.transform = "scale(" + lbZoom + ")";
  if (lbZoom > 1) {
    lightboxImg.classList.add("zoomed");
    lightboxWrap.style.alignItems = "flex-start";
    lightboxWrap.style.justifyContent = "flex-start";
  } else {
    lightboxImg.classList.remove("zoomed");
    lightboxWrap.style.alignItems = "center";
    lightboxWrap.style.justifyContent = "center";
  }
}

function openLightbox() {
  if (!state.photoDataUrl) { return; }
  lightboxImg.src = state.photoDataUrl;
  lbZoom = 1;
  applyZoom();
  lightboxOverlay.classList.add("open");
}
function closeLightbox() {
  lightboxOverlay.classList.remove("open");
  lightboxImg.src = "";
  lbZoom = 1;
  applyZoom();
}
photoThumbRow.addEventListener("click", openLightbox);
lightboxClose.addEventListener("click", closeLightbox);
lightboxOverlay.addEventListener("click", function(e) {
  if (e.target === lightboxOverlay || e.target === lightboxWrap) { closeLightbox(); }
});
lightboxWrap.addEventListener("dblclick", function(e) {
  e.preventDefault();
  if (lbZoom >= 3) { lbZoom = 1; } else { lbZoom = Math.min(lbZoom + 1, 4); }
  applyZoom();
});
lightboxWrap.addEventListener("wheel", function(e) {
  e.preventDefault();
  lbZoom = Math.max(0.5, Math.min(4, lbZoom + (e.deltaY < 0 ? 0.3 : -0.3)));
  applyZoom();
}, { passive: false });

function showPhotoThumbnail() {
  if (state.photoDataUrl) {
    photoThumbImg.src = state.photoDataUrl;
    photoThumbRow.style.display = "";
  } else {
    photoThumbRow.style.display = "none";
  }
}

// ========== EXTRACT ==========
extractBtn.addEventListener("click", function() {
  var hasPhoto = !!state.uploadedFile;
  var userText = smartTextInput.value.trim();
  lastUserInstruction = userText;
  lastInputHadText = !!userText;

  if (!hasPhoto && !userText) {
    showToast("Please upload an image or type a task", "error");
    return;
  }

  if (liteModeCheck.checked) {
    updateStepper(3);
    showSection("sync");
    syncingCard.style.display = "";
    syncDoneCard.style.display = "none";
    syncStatusText.textContent = "Extracting tasks…";
  } else {
    updateStepper(2);
    showSection("review");
    extractionLoading.style.display = "block";
    taskList.innerHTML = "";
    addTaskBtn.style.display = "none";
    reviewActions.style.display = "none";
  }

  (async function() {
    try {
      var formData = new FormData();
      if (userText) { formData.append("text", userText); }
      formData.append("timezone", timezoneSelect.value || "UTC");
      if (state.uploadedFile) { formData.append("image", state.uploadedFile); }

      var draft = await apiFetch("/api/extract", { method: "POST", body: formData });
      applyDraftResponse(draft);
      showPhotoThumbnail();

      if (liteModeCheck.checked) {
        await runLiteModeSync();
        return;
      }

      extractionLoading.style.display = "none";
      renderPhotoDate();
      renderTasks();
      if (state.tasks.length > 0) {
        var unmatchedCount = state.tasks.filter(function(t) { return !t.listName; }).length;
        var msg = "Extracted " + state.tasks.length + " task" + (state.tasks.length > 1 ? "s" : "");
        if (unmatchedCount > 0) { msg += " (" + unmatchedCount + " need a list)"; }
        showToast(msg, "success");
      } else {
        showToast("No tasks found. You can add tasks manually.", "info");
      }
      addTaskBtn.style.display = "";
      reviewActions.style.display = "flex";
    } catch (err) {
      showToast("Extraction failed: " + err.message, "error");
      updateStepper(2);
      showSection("review");
      extractionLoading.style.display = "none";
      reviewActions.style.display = "flex";
      addTaskBtn.style.display = "";
    }
  })();
});

// ========== LITE MODE ==========
// Simplified from the original: routing is fully resolved server-side at
// extraction time now (list_table refactor) -- task_checked already
// reflects the app's date-specific/confidently-routed default, and
// task_list_name is already set wherever the AI/category-default could
// determine it. No client-side list lookup needed here at all; any
// genuinely unmatched task just surfaces via the sync API's existing
// per-task "no list assigned" failure detail.
async function runLiteModeSync() {
  var checkedTasks = state.tasks.filter(function(t) { return t.checked && t.title.trim(); });
  if (checkedTasks.length === 0) {
    // Nothing defaulted to checked -- fall back to full review, same as
    // the original.
    state.tasks.forEach(function(t) { t.checked = true; });
    updateStepper(2);
    showSection("review");
    showToast("No date-specific or confidently-routed tasks found. Showing full review.", "info");
    renderPhotoDate();
    renderTasks();
    addTaskBtn.style.display = "";
    reviewActions.style.display = "flex";
    return;
  }

  var totalCount = state.tasks.length;
  showToast("Lite mode: syncing " + checkedTasks.length + " of " + totalCount + " task" +
    (totalCount > 1 ? "s" : "") + "…", "success");

  try {
    var result = await apiJson("POST", "/api/drafts/" + state.draftId + "/sync", {});
    applyDraftResponse(result.draft);
    showSyncResult(result.results);
  } catch (err) {
    showToast("Sync failed: " + err.message, "error");
    updateStepper(2);
    showSection("review");
    renderTasks();
  }
}

// ========== RENDER PHOTO DATE (read-only -- see file header note) ==========
function renderPhotoDate() {
  if (state.photoDate) {
    photoDateRow.style.display = "";
    photoDateValue.textContent = state.photoDate;
    var nextBiz = getNextBusinessDay(state.photoDate);
    photoDateInfo.textContent = "Tasks without a due date default to next business day: " + nextBiz + " at 09:00";
  } else {
    photoDateRow.style.display = "none";
  }
}

// ========== RENDER TASKS ==========
function renderTaskCard(task, idx, animIdx) {
  var card = document.createElement("div");
  var cls = "task-card";
  if (!task.listName) { cls += " priority-task"; } // reuse the accent styling to flag "needs a list"
  if (!task.checked) { cls += " unchecked-task"; }
  card.className = cls;
  card.style.animationDelay = (animIdx * 0.06) + "s";

  var dueDateOnly = task.dueDateTime ? task.dueDateTime.substring(0, 10) : "";
  var dueTimeOnly = task.dueDateTime ? task.dueDateTime.substring(11, 16) : "";
  var checkedAttr = task.checked ? " checked" : "";

  var listBadgeHtml = "";
  if (task.listName) {
    listBadgeHtml = '<div style="margin-top:4px;"><span style="display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;background:var(--accent-light);color:var(--accent);font-weight:600;"><i class="fas fa-list" style="margin-right:4px;"></i>' + escapeHtml(task.listName) + '</span></div>';
  }

  card.innerHTML =
    '<div class="task-card-header">' +
      '<input type="checkbox" class="task-check" data-id="' + task.id + '"' + checkedAttr + '>' +
      '<input type="text" class="task-title-input" value="' + escapeAttr(task.title) + '" data-id="' + task.id + '" data-field="title" placeholder="Task title">' +
      '<div class="task-actions">' +
        (task.synced ? '<span class="task-synced"><i class="fas fa-check"></i> Synced</span>' : '') +
        '<button class="task-remove" data-id="' + task.id + '" title="Remove task" type="button"><i class="fas fa-trash-can"></i></button>' +
      '</div>' +
    '</div>' +
    '<textarea class="task-body-input" data-id="' + task.id + '" data-field="body" placeholder="Notes (optional)" rows="' + (task.isEvent ? 2 : 1) + '">' + escapeHtml(task.body) + '</textarea>' +
    '<div class="task-meta">' +
      '<i class="fas fa-calendar-day"></i>' +
      '<input type="date" class="task-date-input" data-id="' + task.id + '" value="' + escapeAttr(dueDateOnly) + '" style="font-family:var(--font-sans);font-size:13px;color:var(--text-primary);background:var(--bg-secondary);border:1px solid var(--border);border-radius:6px;padding:4px 8px;outline:none;cursor:pointer;">' +
      '<input type="time" data-id="' + task.id + '" data-field="dueTime" value="' + escapeAttr(dueTimeOnly) + '" style="font-family:var(--font-sans);font-size:13px;color:var(--accent);background:var(--accent-light);border:1px solid var(--border);border-radius:6px;padding:4px 8px;outline:none;margin-left:4px;">' +
    '</div>' +
    listBadgeHtml;

  return card;
}

function findTask(id) {
  return state.tasks.find(function(t) { return t.id === id; });
}

function renderTasks() {
  taskList.innerHTML = "";
  if (state.tasks.length === 0) {
    taskList.innerHTML = '<div class="empty-state"><i class="fas fa-clipboard-list"></i><p>No tasks yet. Add tasks manually or go back to input.</p></div>';
    updateSyncBtnCount();
    return;
  }

  // Dynamic grouping by resolved list name (list_table refactor) -- no
  // more fixed priority/other/event buckets. Tasks with no list yet
  // (listName null -- unmatched) group under "Unmatched", sorted last so
  // it reads as "needs your attention" rather than leading the list.
  var groups = {};
  var order = [];
  state.tasks.forEach(function(task) {
    var key = task.listName || "";
    if (!groups[key]) { groups[key] = []; order.push(key); }
    groups[key].push(task);
  });
  order.sort(function(a, b) {
    if (a === "" && b !== "") { return 1; }
    if (b === "" && a !== "") { return -1; }
    return a.localeCompare(b);
  });

  var animIdx = 0;
  order.forEach(function(key, i) {
    var tasks = groups[key];
    var label = document.createElement("div");
    label.className = key === "" ? "task-section-label priority" : "task-section-label";
    var icon = key === "" ? "fa-triangle-exclamation" : "fa-list-check";
    var title = key === "" ? "Unmatched — needs a list" : escapeHtml(key);
    label.innerHTML = '<i class="fas ' + icon + '"></i> ' + title + " (" + tasks.length + ")";
    taskList.appendChild(label);
    tasks.forEach(function(t) { taskList.appendChild(renderTaskCard(t, null, animIdx++)); });
    if (i < order.length - 1) {
      var divider = document.createElement("div");
      divider.className = "task-section-divider";
      taskList.appendChild(divider);
    }
  });

  // Bind checkbox events -- immediate persist, no debounce (discrete event)
  taskList.querySelectorAll(".task-check").forEach(function(cb) {
    cb.addEventListener("change", function() {
      var task = findTask(this.getAttribute("data-id"));
      if (!task) { return; }
      task.checked = this.checked;
      var card = this.closest(".task-card");
      if (card) {
        if (this.checked) { card.classList.remove("unchecked-task"); }
        else { card.classList.add("unchecked-task"); }
      }
      updateSyncBtnCount();
      apiPatchTaskSafe(task.id, { checked: task.checked });
    });
  });

  // Bind text inputs -- local update on every keystroke, persist on blur.
  // Editing text no longer recomputes/re-groups anything client-side --
  // list routing only happens once, server-side, at extraction time.
  taskList.querySelectorAll(".task-title-input, .task-body-input").forEach(function(el) {
    el.addEventListener("input", function() {
      var task = findTask(this.getAttribute("data-id"));
      var field = this.getAttribute("data-field");
      if (!task) { return; }
      task[field] = this.value;
    });
    el.addEventListener("blur", function() {
      var task = findTask(this.getAttribute("data-id"));
      if (!task) { return; }
      apiPatchTaskSafe(task.id, { title: task.title, body: task.body });
    });
  });

  taskList.querySelectorAll(".task-date-input").forEach(function(el) {
    el.addEventListener("change", function() {
      var task = findTask(this.getAttribute("data-id"));
      if (!task) { return; }
      var dateVal = this.value;
      var timeStr = task.dueDateTime ? task.dueDateTime.substring(11, 16) : "";
      if (dateVal && timeStr) { task.dueDateTime = dateVal + "T" + timeStr + ":00"; }
      else if (dateVal) { task.dueDateTime = dateVal + "T09:00:00"; }
      else { task.dueDateTime = null; }
      apiPatchTaskSafe(task.id, { due_datetime: task.dueDateTime });
    });
  });

  taskList.querySelectorAll("input[data-field='dueTime']").forEach(function(el) {
    el.addEventListener("change", function() {
      var task = findTask(this.getAttribute("data-id"));
      if (!task) { return; }
      var dateStr = task.dueDateTime ? task.dueDateTime.substring(0, 10) : "";
      if (dateStr && this.value) {
        task.dueDateTime = dateStr + "T" + this.value + ":00";
        apiPatchTaskSafe(task.id, { due_datetime: task.dueDateTime });
      }
    });
  });

  taskList.querySelectorAll(".task-remove").forEach(function(btn) {
    btn.addEventListener("click", function() {
      var taskId = this.getAttribute("data-id");
      apiFetch("/api/drafts/" + state.draftId + "/tasks/" + taskId, { method: "DELETE" })
        .then(function(draft) {
          applyDraftResponse(draft);
          renderTasks();
        })
        .catch(function(err) { showToast("Could not remove task: " + err.message, "error"); });
    });
  });

  taskList.querySelectorAll(".task-body-input").forEach(function(ta) {
    autoResizeTextarea(ta);
    ta.addEventListener("input", function() { autoResizeTextarea(ta); });
  });

  updateSyncBtnCount();
}

// Fire-and-forget PATCH with a toast on failure -- keeps the UI responsive
// (the original had nothing to persist at all; this app's equivalent of
// "don't block typing on a network round-trip" is to not await this).
function apiPatchTaskSafe(taskId, fields) {
  apiJson("PATCH", "/api/drafts/" + state.draftId + "/tasks/" + taskId, fields)
    .then(function(draft) { applyDraftResponse(draft); })
    .catch(function(err) { showToast("Could not save edit: " + err.message, "error"); });
}

function updateSyncBtnCount() {
  var checkedCount = state.tasks.filter(function(t) { return t.checked && !t.synced && t.title.trim(); }).length;
  syncBtn.innerHTML = '<i class="fas fa-cloud-arrow-up"></i> Sync ' + checkedCount + ' task' + (checkedCount !== 1 ? 's' : '') + ' to Microsoft To Do';
}

function autoResizeTextarea(ta) {
  ta.style.height = "auto";
  ta.style.height = ta.scrollHeight + "px";
}

function escapeHtml(s) {
  var d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}
function escapeAttr(s) {
  return (s || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ========== ADD TASK MANUALLY ==========
addTaskBtn.addEventListener("click", function() {
  apiJson("POST", "/api/drafts/" + state.draftId + "/tasks", { kind: "task", title: "", checked: true })
    .then(function(draft) {
      applyDraftResponse(draft);
      renderTasks();
      var lastTitle = taskList.querySelector(".task-card:last-child .task-title-input");
      if (lastTitle) { lastTitle.focus(); }
    })
    .catch(function(err) { showToast("Could not add task: " + err.message, "error"); });
});

backToUploadBtn.addEventListener("click", function() {
  updateStepper(1);
  showSection("upload");
});

// ========== LIST MODAL ==========
// Fully dynamic (list_table refactor): one section per list the server
// already resolved (with a "change" select, pre-selected to that list --
// only sent back to the server if actually changed), plus one required
// section for anything the AI couldn't confidently route.
var modalResolvedGroups = {}; // listName -> tasks[]
var modalUnmatchedTasks = []; // tasks[]
var modalResolvedData = []; // { listName, tasks, selectEl }
var modalUnmatchedSelect = null;
var modalUnmatchedInput = null;
var currentLists = [];

function truncateTitle(title, maxLen) {
  maxLen = maxLen || 40;
  if (!title) { return "Untitled task"; }
  if (title.length <= maxLen) { return title; }
  return title.substring(0, maxLen).trimEnd() + "…";
}

function buildModalTaskListHtml(tasks) {
  var html = '<ul class="modal-task-list">';
  tasks.forEach(function(t) { html += "<li>" + escapeHtml(truncateTitle(t.title)) + "</li>"; });
  html += "</ul>";
  return html;
}

function openListModal() {
  var checkedTasks = state.tasks.filter(function(t) { return t.checked && !t.synced && t.title.trim(); });

  var resolvedGroups = {};
  var unmatchedTasks = [];
  checkedTasks.forEach(function(t) {
    if (t.listName) {
      if (!resolvedGroups[t.listName]) { resolvedGroups[t.listName] = []; }
      resolvedGroups[t.listName].push(t);
    } else {
      unmatchedTasks.push(t);
    }
  });
  modalResolvedGroups = resolvedGroups;
  modalUnmatchedTasks = unmatchedTasks;

  var allUnchecked = state.tasks.filter(function(t) { return !t.checked && !t.synced && t.title.trim(); });
  var uncheckedNoteEl = $id("modalUncheckedNote");
  uncheckedNoteEl.innerHTML = allUnchecked.length > 0
    ? '<div class="modal-unchecked-note">' + allUnchecked.length + ' unchecked task' + (allUnchecked.length > 1 ? 's' : '') + ' excluded from sync</div>'
    : "";

  listModal.classList.add("open");
  fetchTaskLists();
}

function closeListModal() { listModal.classList.remove("open"); }
listModalCancel.addEventListener("click", closeListModal);
listModal.addEventListener("click", function(e) { if (e.target === listModal) { closeListModal(); } });

function populateListSelect(selectEl, lists, defaultName) {
  selectEl.innerHTML = "";
  var matchedIdx = -1;
  lists.forEach(function(list, i) {
    var opt = document.createElement("option");
    opt.value = list.displayName; // sync payload is name-based -- server resolves/creates by name
    opt.textContent = list.displayName;
    selectEl.appendChild(opt);
    if (defaultName && list.displayName.toLowerCase() === defaultName.toLowerCase()) { matchedIdx = i; }
  });
  if (matchedIdx !== -1) { selectEl.selectedIndex = matchedIdx; }
  if (lists.length === 0) { selectEl.innerHTML = '<option value="">No lists found</option>'; }
}

async function fetchTaskLists() {
  $id("modalCustomSections").innerHTML = '<p style="font-size:13px; color:var(--text-secondary);">Loading lists…</p>';
  $id("modalUnmatchedSections").innerHTML = "";
  try {
    currentLists = await apiFetch("/api/lists");
    buildModalSections(currentLists);
  } catch (err) {
    var msg = err.isAuthError ? "Not connected — see banner above" : ("Could not load task lists: " + err.message);
    $id("modalCustomSections").innerHTML = '<p style="font-size:13px; color:var(--danger);">' + escapeHtml(msg) + "</p>";
    showToast("Could not load task lists: " + err.message, "error");
  }
}

function buildModalSections(lists) {
  var customContainer = $id("modalCustomSections");
  var unmatchedContainer = $id("modalUnmatchedSections");
  customContainer.innerHTML = "";
  unmatchedContainer.innerHTML = "";
  modalResolvedData = [];
  modalUnmatchedSelect = null;
  modalUnmatchedInput = null;

  Object.keys(modalResolvedGroups).sort().forEach(function(listName) {
    var tasks = modalResolvedGroups[listName];
    var section = document.createElement("div");
    section.className = "modal-custom-section";
    section.innerHTML =
      '<div class="modal-custom-label">' +
        '<i class="fas fa-list" style="color:var(--accent);"></i>' +
        '<span class="custom-matched">' + escapeHtml(listName) + " (" + tasks.length + ")</span>" +
      "</div>" +
      buildModalTaskListHtml(tasks) +
      '<div class="field" style="margin-bottom:8px;"><select class="custom-list-select"></select></div>';
    customContainer.appendChild(section);
    var selectEl = section.querySelector(".custom-list-select");
    populateListSelect(selectEl, lists, listName);
    modalResolvedData.push({ listName: listName, tasks: tasks, selectEl: selectEl });
  });

  if (modalUnmatchedTasks.length > 0) {
    var section2 = document.createElement("div");
    section2.className = "modal-custom-section";
    section2.innerHTML =
      '<div class="modal-custom-label">' +
        '<i class="fas fa-triangle-exclamation" style="color:var(--danger);"></i>' +
        '<span class="custom-unmatched">Unmatched — pick a list (' + modalUnmatchedTasks.length + ")</span>" +
      "</div>" +
      buildModalTaskListHtml(modalUnmatchedTasks) +
      '<div class="modal-unmatched-hint">These tasks weren\'t confidently routed to a list. Pick an existing one or create a new list.</div>' +
      '<div class="field" style="margin-bottom:8px;"><select class="unmatched-list-select"><option value="__create__">Create new list</option></select></div>' +
      '<div class="field" style="margin-bottom:16px;"><input type="text" class="modal-new-list-input" placeholder="New list name"></div>';
    unmatchedContainer.appendChild(section2);
    var selectEl2 = section2.querySelector(".unmatched-list-select");
    var inputEl = section2.querySelector(".modal-new-list-input");
    lists.forEach(function(list) {
      var opt = document.createElement("option");
      opt.value = list.displayName;
      opt.textContent = list.displayName;
      selectEl2.appendChild(opt);
    });
    selectEl2.addEventListener("change", function() {
      if (selectEl2.value === "__create__") { inputEl.disabled = false; inputEl.style.opacity = ""; }
      else { inputEl.disabled = true; inputEl.style.opacity = "0.5"; }
    });
    modalUnmatchedSelect = selectEl2;
    modalUnmatchedInput = inputEl;
  }
}

// ========== SYNC FLOW ==========
syncBtn.addEventListener("click", function() {
  var checkedUnsynced = state.tasks.filter(function(t) { return t.checked && !t.synced && t.title.trim(); });
  if (checkedUnsynced.length === 0) {
    showToast("No checked tasks to sync", "error");
    return;
  }
  openListModal();
});

listModalConfirm.addEventListener("click", function() {
  if (modalUnmatchedSelect && modalUnmatchedSelect.value === "__create__" && !modalUnmatchedInput.value.trim()) {
    showToast("Please enter a name for the new list", "error");
    return;
  }

  closeListModal();

  // Build task_id -> list name assignments -- only for tasks the user
  // actually changed away from the server-resolved list, plus every
  // unmatched task (required). Anything left alone syncs using whatever
  // task_list_name is already stored on it -- no assignment needed.
  var listAssignments = {};
  modalResolvedData.forEach(function(rd) {
    if (rd.selectEl.value !== rd.listName) {
      rd.tasks.forEach(function(t) { listAssignments[t.id] = rd.selectEl.value; });
    }
  });
  if (modalUnmatchedTasks.length > 0) {
    var name = modalUnmatchedSelect.value === "__create__" ? modalUnmatchedInput.value.trim() : modalUnmatchedSelect.value;
    modalUnmatchedTasks.forEach(function(t) { listAssignments[t.id] = name; });
  }

  updateStepper(3);
  showSection("sync");
  syncingCard.style.display = "";
  syncDoneCard.style.display = "none";

  apiJson("POST", "/api/drafts/" + state.draftId + "/sync", { list_assignments: listAssignments })
    .then(function(result) {
      applyDraftResponse(result.draft);
      showSyncResult(result.results);
    })
    .catch(function(err) {
      showToast("Sync failed: " + err.message, "error");
      updateStepper(2);
      showSection("review");
      renderTasks();
    });
});

// End-of-sync UI. Simplified from the original: one API call now does the
// whole batch server-side, so there's no per-task "task N of M" progress
// text to show while it's in flight -- just an indeterminate bar.
function showSyncResult(results) {
  syncingCard.style.display = "none";
  syncDoneCard.style.display = "";
  reAuthBtn.style.display = "none";
  retryFailedBtn.style.display = "none";
  syncErrorDetail.style.display = "none";
  syncErrorDetail.innerHTML = "";

  var done = results.filter(function(r) { return r.status === "synced"; }).length;
  var errors = results.filter(function(r) { return r.status !== "synced"; }).length;
  var authError = results.some(function(r) { return r.status === "failed" && /401|not linked|auth/i.test(r.detail || ""); });

  if (errors === 0) {
    syncDoneIcon.innerHTML = '<i class="fas fa-circle-check"></i>';
    syncDoneIcon.style.color = "var(--success)";
    syncDoneTitle.textContent = "All synced!";
    syncDoneText.textContent = done + " task" + (done > 1 ? "s" : "") + " synced to Microsoft To Do.";
  } else {
    syncDoneIcon.innerHTML = '<i class="fas fa-triangle-exclamation"></i>';
    syncDoneIcon.style.color = "var(--danger)";
    syncDoneTitle.textContent = done > 0 ? "Partially synced" : "Sync failed";
    syncDoneText.textContent = done + " synced, " + errors + " failed.";

    var firstFailed = results.find(function(r) { return r.status !== "synced"; });
    if (firstFailed) {
      syncErrorDetail.style.display = "";
      syncErrorDetail.innerHTML = '<div class="error-detail"><strong><i class="fas fa-circle-exclamation"></i> Sync error</strong>' + escapeHtml(firstFailed.detail || "Unknown error") + '</div>';
    }
    if (authError) { reAuthBtn.style.display = ""; }
    retryFailedBtn.style.display = "";
  }
}

// ========== RE-AUTH / RETRY / NEW SCAN ==========
reAuthBtn.addEventListener("click", function() {
  window.location.href = "/auth/login";
});

retryFailedBtn.addEventListener("click", function() {
  updateStepper(2);
  showSection("review");
  renderTasks();
});

newScanBtn.addEventListener("click", function() {
  state.draftId = null;
  state.tasks = [];
  state.photoDate = null;
  state.uploadedFile = null;
  state.photoDataUrl = null;
  previewImg.src = "";
  uploadPreview.style.display = "none";
  uploadZone.style.display = "";
  cameraRow.style.display = "";
  imageInput.value = "";
  cameraInput.value = "";
  smartTextInput.value = "";
  smartPasteDataUrl = null;
  smartPastePreview.style.display = "none";
  liteModeCheck.checked = false;
  updateExtractBtnState();
  updateStepper(1);
  showSection("upload");
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

function populateTimezoneSelect() {
  var detected = (Intl.DateTimeFormat().resolvedOptions().timeZone) || "UTC";
  var common = ["Asia/Hong_Kong", "Asia/Tokyo", "Asia/Shanghai", "Asia/Singapore",
    "Europe/London", "America/New_York", "America/Los_Angeles", "UTC"];
  if (common.indexOf(detected) === -1) { common.unshift(detected); }
  timezoneSelect.innerHTML = "";
  common.forEach(function(tz) {
    var opt = document.createElement("option");
    opt.value = tz;
    opt.textContent = tz;
    if (tz === detected) { opt.selected = true; }
    timezoneSelect.appendChild(opt);
  });
}

(async function init() {
  populateTimezoneSelect();
  updateStepper(1);
  showSection("upload");

  try {
    var config = await apiFetch("/api/config");
    state.msLinked = !!config.ms_linked;
    msLinkNotice.style.display = state.msLinked ? "none" : "";
  } catch (ignored) { /* keep UI usable even if config can't be read */ }
})();
