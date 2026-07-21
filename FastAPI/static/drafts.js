// My Drafts page. Same apiFetch/showToast pattern as app.js/settings.js --
// not shared as a common module since none of these files are large
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

function escapeHtml(s) {
  var d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

var STATUS_LABELS = {
  open: { text: "Open", color: "var(--accent)", bg: "var(--accent-light)" },
  synced: { text: "Synced", color: "var(--success)", bg: "var(--success-light)" },
  abandoned: { text: "Abandoned", color: "var(--text-tertiary)", bg: "var(--bg-secondary)" },
};

var SOURCE_LABELS = { photo: "Photo", text: "Text", photo_text: "Photo + Text" };

function formatCreated(isoStr) {
  try {
    return new Date(isoStr).toLocaleString();
  } catch (ignored) {
    return isoStr;
  }
}

function renderDraftCard(draft) {
  var status = STATUS_LABELS[draft.draft_status] || STATUS_LABELS.open;
  var sourceLabel = SOURCE_LABELS[draft.draft_source] || draft.draft_source;

  var card = document.createElement("div");
  card.className = "list-entry-row"; // reuse the Manage Lists row styling -- same card-in-a-card shape
  card.innerHTML =
    '<div style="display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-bottom:10px;">' +
      '<div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">' +
        '<span style="display:inline-block; font-size:12px; font-weight:700; padding:3px 10px; border-radius:10px; color:' + status.color + '; background:' + status.bg + ';">' + status.text + '</span>' +
        '<span style="font-size:13px; color:var(--text-secondary);"><i class="fas fa-' + (draft.draft_source === "photo" ? "camera-retro" : draft.draft_source === "text" ? "keyboard" : "images") + '" style="margin-right:5px;"></i>' + escapeHtml(sourceLabel) + '</span>' +
        '<span style="font-size:13px; color:var(--text-secondary);">' + draft.task_count + ' task' + (draft.task_count !== 1 ? "s" : "") + '</span>' +
      '</div>' +
      '<span style="font-size:12px; color:var(--text-tertiary);">' + escapeHtml(formatCreated(draft.draft_create_datetime_UTC)) + '</span>' +
    '</div>' +
    '<div class="list-entry-actions">' +
      (draft.draft_status === "open"
        ? '<a class="btn btn-primary btn-sm draft-resume" href="/?draft=' + encodeURIComponent(draft.draft_id) + '"><i class="fas fa-arrow-rotate-right"></i> Resume</a>'
        : "") +
      '<button type="button" class="btn btn-outline btn-sm draft-delete"><i class="fas fa-trash-can"></i> Delete</button>' +
    '</div>';

  card.querySelector(".draft-delete").addEventListener("click", function() {
    if (!window.confirm("Delete this draft" + (draft.task_count > 0 ? " and its " + draft.task_count + " task" + (draft.task_count !== 1 ? "s" : "") : "") + "? This can't be undone.")) {
      return;
    }
    apiFetch("/api/drafts/" + draft.draft_id, { method: "DELETE" })
      .then(function() {
        showToast("Draft deleted", "success");
        loadDrafts();
      })
      .catch(function(err) { showToast("Could not delete draft: " + err.message, "error"); });
  });

  return card;
}

async function loadDrafts() {
  var container = $id("draftsContainer");
  try {
    var drafts = await apiFetch("/api/drafts");
    container.innerHTML = "";
    if (drafts.length === 0) {
      container.innerHTML = '<div class="empty-state"><i class="fas fa-clipboard-list"></i><p>No drafts yet. Extractions you haven\'t fully synced will show up here.</p></div>';
      return;
    }
    drafts.forEach(function(d) { container.appendChild(renderDraftCard(d)); });
  } catch (err) {
    container.innerHTML = '<p style="font-size:13px; color:var(--danger);">Could not load drafts: ' + escapeHtml(err.message) + "</p>";
  }
}

loadDrafts();
