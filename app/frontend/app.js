/* AP Reconcile workbench controller supporting keyboard navigation, filters, and lightbox preview */

const STATUS_ORDER = [
  "MATCHED",
  "MATCHED_WITHIN_TOLERANCE",
  "AMOUNT_MISMATCH",
  "MISSING_IN_ERP",
  "MISSING_IN_PDF",
  "DUPLICATE_IN_ERP",
  "DUPLICATE_IN_PDF",
  "NEEDS_REVIEW",
];

let currentRunId = null;
let currentReport = null;
let activeFilter = "EXCEPTIONS";
let searchQuery = "";
let selectedNormKey = null;

const fmtIdr = (val) => Number(val || 0).toLocaleString("en-US", { maximumFractionDigits: 0 });
const fmtSignedIdr = (val) => {
  const n = Number(val || 0);
  if (n > 0) return `+${fmtIdr(n)}`;
  return fmtIdr(n);
};

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function getVisibleDocuments(report) {
  const q = searchQuery.trim().toLowerCase();
  return report.documents.filter((doc) => {
    const isMatchOk = doc.status === "MATCHED" || doc.status === "MATCHED_WITHIN_TOLERANCE";
    if (activeFilter === "EXCEPTIONS" && isMatchOk) return false;
    if (activeFilter !== "ALL" && activeFilter !== "EXCEPTIONS" && doc.status !== activeFilter) return false;
    if (!q) return true;
    const hay = [
      doc.pdf_doc_number_raw,
      doc.erp_doc_number_raw,
      doc.pdf_po_or_ref,
      doc.erp_po_or_ref,
      ...(doc.reason_codes || []),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return hay.includes(q);
  });
}

async function loadHealth() {
  try {
    const res = await fetch("/health");
    const data = await res.json();
    const fallbackNote = data.fallback_model ? ` | fallback: ${data.fallback_model}` : "";
    document.getElementById("health-meta").textContent = `${data.model}${fallbackNote}`;
  } catch {
    document.getElementById("health-meta").textContent = "Engine unreachable";
  }
}

function bindDropzone(inputId, zoneId, labelId, metaId, emptyTitle) {
  const input = document.getElementById(inputId);
  const zone = document.getElementById(zoneId);
  const label = document.getElementById(labelId);
  const meta = document.getElementById(metaId);

  const updateLabel = () => {
    const file = input.files && input.files[0];
    if (file) {
      zone.classList.add("has-file");
      label.textContent = file.name;
      meta.textContent = `${Math.max(1, Math.round(file.size / 1024))} KB ready`;
    } else {
      zone.classList.remove("has-file");
      label.textContent = emptyTitle;
    }
  };

  input.addEventListener("change", updateLabel);
  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("dragover");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("dragover");
    const dropped = e.dataTransfer && e.dataTransfer.files;
    if (dropped && dropped.length > 0) {
      const transfer = new DataTransfer();
      transfer.items.add(dropped[0]);
      input.files = transfer.files;
      updateLabel();
    }
  });
}

function showError(title, detail, issues) {
  const box = document.getElementById("error-box");
  document.getElementById("error-title").textContent = title;
  let html = `<p style="margin:0 0 8px;">${escapeHtml(detail)}</p>`;
  if (issues && issues.length) {
    html += `<table><thead><tr><th>Sheet</th><th>Row</th><th>Column</th><th>Raw Value</th><th>Issue</th></tr></thead><tbody>`;
    for (const item of issues) {
      html += `<tr><td>${escapeHtml(item.sheet)}</td><td>${item.row ?? ""}</td><td>${escapeHtml(item.column || "")}</td><td>${escapeHtml(item.raw_value || "")}</td><td>${escapeHtml(item.message)}</td></tr>`;
    }
    html += `</tbody></table>`;
  }
  document.getElementById("error-body").innerHTML = html;
  box.classList.remove("hidden");
}

function hideError() {
  document.getElementById("error-box").classList.add("hidden");
}

function renderTieOutAndBridge(report) {
  const ct = report.control_totals;
  const unexplained = Number(ct.unexplained_variance);
  const cells = [
    { label: "PDF Net Total", value: `Rp ${fmtIdr(ct.raw_pdf_net_total)}`, sub: `${ct.pdf_document_count} PDF documents` },
    {
      label: "ERP Net Total",
      value: `Rp ${fmtIdr(ct.raw_erp_net_total)}`,
      sub: `Posted Rp ${fmtIdr(ct.erp_posted_net_total)} | Parked Rp ${fmtIdr(ct.erp_parked_net_total)}`,
    },
    {
      label: "Net Variance (PDF - ERP)",
      value: `Rp ${fmtSignedIdr(ct.net_difference)}`,
      sub: `Explained Rp ${fmtSignedIdr(ct.total_explained_difference)}`,
    },
    {
      label: "Unexplained Variance",
      value: `Rp ${fmtIdr(unexplained)}`,
      sub: unexplained === 0 ? "Control tie-out balanced" : "Variance requires check",
      valueClass: unexplained === 0 ? "tieout-ok" : "tieout-bad",
    },
  ];

  document.getElementById("tieout-strip").innerHTML = cells
    .map(
      (c) => `
      <div class="tieout-cell">
        <div class="tieout-label">${escapeHtml(c.label)}</div>
        <div class="tieout-value ${c.valueClass || ""}">${escapeHtml(c.value)}</div>
        <div class="tieout-sub">${escapeHtml(c.sub)}</div>
      </div>
    `
    )
    .join("");

  const counts = ct.status_counts || {};
  const explained = ct.explained_by_status || {};
  const bridgeParts = STATUS_ORDER.filter((st) => (counts[st] || 0) > 0).map(
    (st) => `
      <span class="bridge-item">
        <span class="badge st-${escapeHtml(st)}">${escapeHtml(st)} (${counts[st]})</span>
        <span>Rp ${fmtSignedIdr(explained[st] || 0)}</span>
      </span>
    `
  );
  document.getElementById("bridge-strip").innerHTML = `<span class="bridge-title">Variance Bridge</span>${bridgeParts.join("")}`;

  const u = report.usage;
  if (u) {
    const totalIn = u.input_tokens + u.retry_input_tokens;
    const totalOut = u.output_tokens + u.retry_output_tokens;
    const costTxt = u.estimated_cost_usd != null ? ` | $${Number(u.estimated_cost_usd).toFixed(4)}` : "";
    const fbTxt = u.fallback_calls > 0 && u.fallback_model ? ` | fallback ${u.fallback_calls}x (${u.fallback_model})` : "";
    document.getElementById("usage-line").textContent =
      `${u.model}${fbTxt} | ${(u.latency_ms / 1000).toFixed(1)}s | ${totalIn.toLocaleString()} in / ${totalOut.toLocaleString()} out${costTxt}`;
  }
}

function renderPills(report) {
  const counts = report.control_totals.status_counts || {};
  const excCount = report.documents.filter((d) => d.status !== "MATCHED" && d.status !== "MATCHED_WITHIN_TOLERANCE").length;
  const order = ["EXCEPTIONS", "ALL", ...STATUS_ORDER];

  const html = order
    .map((key) => {
      const cnt = key === "ALL" ? report.documents.length : key === "EXCEPTIONS" ? excCount : counts[key] || 0;
      if (key !== "ALL" && key !== "EXCEPTIONS" && cnt === 0) return "";
      const label = key === "ALL" ? `All (${cnt})` : key === "EXCEPTIONS" ? `Exceptions (${cnt})` : `${key} (${cnt})`;
      return `<button type="button" class="pill ${activeFilter === key ? "active" : ""}" data-filter="${key}">${escapeHtml(label)}</button>`;
    })
    .join("");

  const container = document.getElementById("status-pills");
  container.innerHTML = html;
  container.querySelectorAll(".pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      activeFilter = btn.dataset.filter;
      const visible = getVisibleDocuments(currentReport);
      if (!visible.some((d) => d.doc_number_norm === selectedNormKey)) {
        selectedNormKey = visible[0] ? visible[0].doc_number_norm : null;
      }
      renderPills(currentReport);
      renderTable(currentReport);
      const doc = currentReport.documents.find((x) => x.doc_number_norm === selectedNormKey);
      if (doc) renderDetail(doc, doc.pdf_pages[0] || null);
    });
  });
}

function renderTable(report) {
  const visible = getVisibleDocuments(report);
  const tbody = document.getElementById("docs-tbody");

  if (!visible.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted">No documents match the current filter.</td></tr>`;
    return;
  }

  tbody.innerHTML = visible
    .map((d) => {
      const label = d.pdf_doc_number_raw || d.erp_doc_number_raw || d.doc_number_norm;
      const isSelected = d.doc_number_norm === selectedNormKey;
      const erpSummary = d.erp_refs.length ? escapeHtml(d.erp_refs.join(", ")) : "None";
      return `
        <tr class="clickable ${isSelected ? "selected" : ""}" data-key="${escapeHtml(d.doc_number_norm)}">
          <td>
            <strong>${escapeHtml(label)}</strong>
            <div class="muted">${escapeHtml(d.document_type)}${d.pdf_po_or_ref ? ` | ${escapeHtml(d.pdf_po_or_ref)}` : ""}</div>
          </td>
          <td><span class="badge st-${escapeHtml(d.status)}">${escapeHtml(d.status)}</span></td>
          <td>${d.pdf_pages.length ? d.pdf_pages.map((p) => `P${p}`).join(", ") : "None"}</td>
          <td>${erpSummary}</td>
          <td class="num">${fmtIdr(d.pdf_total_signed)}</td>
          <td class="num">${fmtIdr(d.erp_total_signed)}</td>
          <td class="num">${fmtSignedIdr(d.difference_signed)}</td>
        </tr>
      `;
    })
    .join("");

  tbody.querySelectorAll("tr.clickable").forEach((row) => {
    row.addEventListener("click", () => selectDocument(row.dataset.key));
  });
}

function selectDocument(normKey) {
  if (!currentReport) return;
  selectedNormKey = normKey;
  renderTable(currentReport);
  const doc = currentReport.documents.find((x) => x.doc_number_norm === selectedNormKey);
  if (doc) renderDetail(doc, doc.pdf_pages[0] || null);
}

function buildHeaderAndErpPeekHtml(doc, auditTrail) {
  const docRefs = (auditTrail || []).filter((ref) => ref.doc_number_norm === doc.doc_number_norm);
  const sumHeaderField = (side, fieldName) => {
    const matching = docRefs.filter(
      (r) => r.source_side === side && r.level === "HEADER" && r.field_or_item === fieldName && r.numeric_value != null
    );
    if (!matching.length) return null;
    return matching.reduce((acc, r) => acc + Number(r.numeric_value), 0);
  };

  const headerFields = ["Subtotal", "DPP", "PPN", "Grand Total"];
  const headerRowsHtml = headerFields
    .map((field) => {
      const pdfVal = sumHeaderField("PDF", field);
      const erpVal = sumHeaderField("ERP", field);
      const diffVal = (pdfVal ?? 0) - (erpVal ?? 0);
      return `
        <tr>
          <td><strong>${escapeHtml(field)}</strong></td>
          <td class="num">${pdfVal != null ? fmtIdr(pdfVal) : "-"}</td>
          <td class="num">${erpVal != null ? fmtIdr(erpVal) : "-"}</td>
          <td class="num">${pdfVal != null || erpVal != null ? fmtSignedIdr(diffVal) : "-"}</td>
        </tr>
      `;
    })
    .join("");

  const erpLineRefs = docRefs.filter((r) => r.source_side === "ERP" && r.level === "LINE");
  const renderErpRows = (rows) =>
    rows
      .map(
        (r) => `
      <tr>
        <td><span class="muted">${escapeHtml(r.location)}</span></td>
        <td><strong>${escapeHtml(r.field_or_item)}</strong><div class="muted">${escapeHtml(r.raw_value)}</div></td>
        <td class="num">${r.numeric_value != null ? fmtIdr(r.numeric_value) : "-"}</td>
      </tr>
    `
      )
      .join("");

  let erpPeekHtml = `<div class="inspector-section"><h3>ERP Workbook Rows</h3><p class="muted" style="margin:0;">No matching entry in ERP workbook.</p></div>`;
  if (doc.erp_refs.length || erpLineRefs.length) {
    const visibleLines = erpLineRefs.slice(0, 5);
    const hiddenLines = erpLineRefs.slice(5);
    const headerLoc = doc.erp_refs.length ? `Header: ${escapeHtml(doc.erp_refs.join(", "))}` : "";
    erpPeekHtml = `
      <div class="inspector-section">
        <h3>ERP Workbook Rows (${erpLineRefs.length} line${erpLineRefs.length === 1 ? "" : "s"})</h3>
        ${headerLoc ? `<div class="muted" style="margin-bottom:6px;">${headerLoc}</div>` : ""}
        ${
          visibleLines.length
            ? `<table class="compact-table">
                <thead><tr><th>Sheet!Row</th><th>Booked Item</th><th class="num">Amount</th></tr></thead>
                <tbody>${renderErpRows(visibleLines)}</tbody>
              </table>`
            : ""
        }
        ${
          hiddenLines.length
            ? `<details class="erp-more">
                <summary>Show ${hiddenLines.length} more ERP row${hiddenLines.length === 1 ? "" : "s"}</summary>
                <table class="compact-table"><tbody>${renderErpRows(hiddenLines)}</tbody></table>
              </details>`
            : ""
        }
      </div>
    `;
  }

  return `
    <div class="inspector-section">
      <h3>Header Totals (PDF vs ERP)</h3>
      <table class="compact-table">
        <thead><tr><th>Field</th><th class="num">PDF</th><th class="num">ERP</th><th class="num">Diff</th></tr></thead>
        <tbody>${headerRowsHtml}</tbody>
      </table>
    </div>
    ${erpPeekHtml}
  `;
}

function renderDetail(doc, activePage) {
  const panel = document.getElementById("detail-panel");
  const title = doc.pdf_doc_number_raw || doc.erp_doc_number_raw || doc.doc_number_norm;

  let checksHtml = "";
  if (doc.checks && doc.checks.length) {
    const chips = doc.checks
      .map(
        (c) =>
          `<span class="check-chip ${c.passed ? "check-pass" : "check-fail"}" title="${escapeHtml(c.detail)}">${escapeHtml(c.name)}</span>`
      )
      .join("");
    checksHtml = `<div class="inspector-section"><h3>Verification Checks</h3><div class="check-list">${chips}</div></div>`;
  }

  let linesHtml = "";
  if (doc.line_discrepancies && doc.line_discrepancies.length) {
    linesHtml = `
      <div class="inspector-section">
        <h3>Line Discrepancies</h3>
        <table>
          <thead><tr><th>Item</th><th>Issue</th><th class="num">PDF</th><th class="num">ERP</th><th class="num">Diff</th></tr></thead>
          <tbody>
            ${doc.line_discrepancies
              .map(
                (ld) => `
              <tr>
                <td><strong>${escapeHtml(ld.item_code || "NO_SKU")}</strong><div class="muted">${escapeHtml(ld.detail)}</div></td>
                <td>${escapeHtml(ld.discrepancy_type)}</td>
                <td class="num">${ld.pdf_line_amount != null ? fmtIdr(ld.pdf_line_amount) : "-"}</td>
                <td class="num">${ld.erp_line_amount != null ? fmtIdr(ld.erp_line_amount) : "-"}</td>
                <td class="num">${fmtSignedIdr(ld.amount_difference)}</td>
              </tr>
            `
              )
              .join("")}
          </tbody>
        </table>
      </div>
    `;
  }

  let previewHtml = "";
  if (doc.pdf_pages && doc.pdf_pages.length && currentRunId) {
    const chosen = activePage || doc.pdf_pages[0];
    const tabs =
      doc.pdf_pages.length > 1
        ? `<div class="page-tabs">${doc.pdf_pages.map((p) => `<button type="button" class="pill ${p === chosen ? "active" : ""}" data-page="${p}">Page ${p}</button>`).join("")}</div>`
        : "";
    const imgUrl = `/api/runs/${currentRunId}/pages/${chosen}`;
    previewHtml = `
      <div class="inspector-section">
        <h3>Source PDF Preview (click image to zoom)</h3>
        ${tabs}
        <img class="page-preview" id="preview-img" src="${imgUrl}" data-caption="${escapeHtml(title)} - Page ${chosen}" alt="PDF page ${chosen}" />
      </div>
    `;
  }

  panel.innerHTML = `
    <div class="inspector-head">
      <div>
        <h2>${escapeHtml(title)}</h2>
        <div class="muted">
          PO / Ref: PDF=${escapeHtml(doc.pdf_po_or_ref || "none")} | ERP=${escapeHtml(doc.erp_po_or_ref || "none")}
          ${doc.erp_doc_status ? ` | ERP Status: ${escapeHtml(doc.erp_doc_status)}` : ""}
        </div>
      </div>
      <span class="badge st-${escapeHtml(doc.status)}">${escapeHtml(doc.status)}</span>
    </div>
    ${doc.reason_codes.length ? `<div class="muted" style="margin-bottom:10px;">Reason codes: ${escapeHtml(doc.reason_codes.join(", "))}</div>` : ""}
    <div class="action-callout">
      <p><strong>Finding:</strong> ${escapeHtml(doc.plain_explanation)}</p>
      <p><strong>Next Action:</strong> ${escapeHtml(doc.suggested_action)}</p>
    </div>
    ${buildHeaderAndErpPeekHtml(doc, currentReport ? currentReport.audit_trail : [])}
    ${linesHtml}
    ${checksHtml}
    ${previewHtml}
  `;

  panel.querySelectorAll("[data-page]").forEach((btn) => {
    btn.addEventListener("click", () => renderDetail(doc, Number(btn.dataset.page)));
  });

  const img = panel.querySelector("#preview-img");
  if (img) {
    img.addEventListener("click", () => openLightbox(img.src, img.dataset.caption));
  }
}

function openLightbox(src, caption) {
  document.getElementById("lightbox-img").src = src;
  document.getElementById("lightbox-caption").textContent = caption || "Source page";
  document.getElementById("lightbox").classList.remove("hidden");
}

function closeLightbox() {
  document.getElementById("lightbox").classList.add("hidden");
}

function bindEvents() {
  bindDropzone("pdf-input", "pdf-dropzone", "pdf-label", "pdf-meta", "Choose or drop .pdf batch");
  bindDropzone("erp-input", "erp-dropzone", "erp-label", "erp-meta", "Choose or drop .xlsx export");

  document.getElementById("dismiss-error-btn").addEventListener("click", hideError);
  document.getElementById("lightbox-close").addEventListener("click", closeLightbox);
  document.getElementById("lightbox").addEventListener("click", (e) => {
    if (e.target.id === "lightbox") closeLightbox();
  });

  document.getElementById("toggle-upload-btn").addEventListener("click", () => {
    document.getElementById("upload-panel").classList.toggle("hidden");
  });

  document.getElementById("search-input").addEventListener("input", (e) => {
    searchQuery = e.target.value;
    if (currentReport) renderTable(currentReport);
  });

  document.getElementById("download-btn").addEventListener("click", () => {
    if (currentRunId) window.location.href = `/api/runs/${currentRunId}/download/xlsx`;
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeLightbox();
    if (!currentReport || (e.key !== "ArrowDown" && e.key !== "ArrowUp")) return;
    if (document.activeElement && document.activeElement.tagName === "INPUT") return;
    const visible = getVisibleDocuments(currentReport);
    if (!visible.length) return;
    e.preventDefault();
    const curIdx = visible.findIndex((d) => d.doc_number_norm === selectedNormKey);
    const nextIdx =
      e.key === "ArrowDown" ? Math.min(visible.length - 1, Math.max(0, curIdx + 1)) : Math.max(0, curIdx - 1);
    selectDocument(visible[nextIdx].doc_number_norm);
  });

  document.getElementById("upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    hideError();
    const pdfFile = document.getElementById("pdf-input").files[0];
    const erpFile = document.getElementById("erp-input").files[0];
    if (!pdfFile || !erpFile) return;

    const btn = document.getElementById("run-btn");
    const status = document.getElementById("run-status");
    btn.disabled = true;
    status.textContent = "Validating ERP workbook and extracting PDF pages...";

    const form = new FormData();
    form.append("pdf_file", pdfFile);
    form.append("erp_file", erpFile);

    try {
      const res = await fetch("/api/reconcile", { method: "POST", body: form });
      const rawText = await res.text();
      let payload = {};
      try {
        payload = rawText ? JSON.parse(rawText) : {};
      } catch {
        payload = { detail: rawText || res.statusText };
      }
      if (!res.ok) {
        showError(
          payload.error === "erp_validation_failed" ? "ERP Workbook Schema Validation Failed" : "Reconciliation Error",
          payload.detail || "Request failed",
          payload.issues || []
        );
        status.textContent = "";
        return;
      }

      currentRunId = payload.run_id;
      currentReport = payload.report;
      const excCount = currentReport.documents.filter(
        (d) => d.status !== "MATCHED" && d.status !== "MATCHED_WITHIN_TOLERANCE"
      ).length;
      activeFilter = excCount > 0 ? "EXCEPTIONS" : "ALL";
      searchQuery = "";
      document.getElementById("search-input").value = "";

      document.getElementById("batch-scope").textContent = `${currentReport.pdf_filename} vs ${currentReport.erp_filename}`;
      document.getElementById("upload-panel").classList.add("hidden");
      document.getElementById("toggle-upload-btn").classList.remove("hidden");
      document.getElementById("results-area").classList.remove("hidden");

      const visible = getVisibleDocuments(currentReport);
      selectedNormKey = visible[0] ? visible[0].doc_number_norm : currentReport.documents[0]?.doc_number_norm || null;

      renderTieOutAndBridge(currentReport);
      renderPills(currentReport);
      renderTable(currentReport);
      if (selectedNormKey) selectDocument(selectedNormKey);
      status.textContent = `Completed run ${currentRunId}.`;
    } catch (err) {
      showError("Network Error", String(err), []);
      status.textContent = "";
    } finally {
      btn.disabled = false;
    }
  });
}

bindEvents();
loadHealth();
