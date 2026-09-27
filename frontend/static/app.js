(() => {
  const status = document.querySelector("[data-job-status-url]");
  if (!status || status.dataset.terminal === "true") return;
  const badge = document.querySelector("[data-job-state-badge]");
  let stopped = false;

  const schedule = () => {
    if (!stopped) window.setTimeout(refresh, 2500);
  };

  const refresh = async () => {
    status.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(status.dataset.jobStatusUrl, {
        headers: { "X-Requested-With": "FoxDenMusic" },
        cache: "no-store"
      });
      if (response.ok) status.innerHTML = await response.text();

      const apiUrl = status.dataset.jobStatusUrl
        .replace(/\/status$/, "")
        .replace(/^\/jobs\//, "/api/jobs/");
      const apiResponse = await fetch(apiUrl, { cache: "no-store" });
      if (apiResponse.ok) {
        const job = await apiResponse.json();
        if (badge && typeof job.state === "string") {
          const stateClass = job.state.toLowerCase().replace(/[^a-z0-9_-]/g, "");
          badge.className = `status large status-${stateClass}`;
          badge.textContent = job.state.replace(/_/g, " ");
        }
        const terminal = ["FAILED", "NEEDS_REVIEW"].includes(job.state) ||
          (job.state === "COMPLETE" && !job.jellyfin_retry_requested);
        status.dataset.terminal = terminal ? "true" : "false";
        if (terminal) {
          stopped = true;
          window.location.reload();
        }
      }
    } catch (_) {
      // A later poll will recover from a transient connection loss.
    } finally {
      status.setAttribute("aria-busy", "false");
      schedule();
    }
  };

  schedule();
})();

(() => {
  const pollers = document.querySelectorAll("[data-poll-url]");
  pollers.forEach((poller) => {
    let stopped = false;
    let timer = null;
    const interval = Math.max(Number(poller.dataset.pollMs) || 10000, 2500);

    const schedule = () => {
      if (!stopped) timer = window.setTimeout(refresh, interval);
    };

    async function refresh() {
      if (document.hidden) {
        schedule();
        return;
      }
      poller.setAttribute("aria-busy", "true");
      try {
        const response = await fetch(poller.dataset.pollUrl, {
          headers: { "X-Requested-With": "FoxDenMusic" },
          cache: "no-store"
        });
        if (response.ok) {
          poller.innerHTML = await response.text();
          const marker = poller.querySelector("[data-poll-terminal]");
          stopped = marker && marker.dataset.pollTerminal === "true";
        }
      } catch (_) {
        // A later poll recovers from a transient connection loss.
      } finally {
        poller.setAttribute("aria-busy", "false");
        schedule();
      }
    }

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && !stopped && timer === null) schedule();
    });
    schedule();
  });
})();

(() => {
  const feedback = document.querySelector("[data-copy-feedback]");
  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", async () => {
      const field = document.getElementById(button.dataset.copyTarget);
      if (!field) return;
      let copied = false;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(field.value);
          copied = true;
        } else {
          field.focus();
          field.select();
          copied = document.execCommand("copy");
        }
      } catch (_) {
        copied = false;
      }
      if (feedback) {
        feedback.textContent = copied
          ? "Spotify URL copied."
          : "Copy was blocked by the browser. The URL is selected so you can copy it manually.";
      }
      if (!copied) {
        field.focus();
        field.select();
      }
    });
  });
})();

(() => {
  const formatBytes = (bytes) => {
    if (!Number.isFinite(bytes) || bytes < 0) return "unknown size";
    const units = ["B", "KiB", "MiB", "GiB"];
    let value = bytes;
    let unit = units[0];
    for (let index = 1; index < units.length && value >= 1024; index += 1) {
      value /= 1024;
      unit = units[index];
    }
    const digits = unit === "B" ? 0 : 1;
    return `${value.toFixed(digits)} ${unit}`;
  };

  document.querySelectorAll("[data-upload-form]").forEach((form) => {
    const fileInput = form.querySelector("[data-upload-file]");
    const selection = form.querySelector("[data-upload-selection]");
    const progressWrap = form.querySelector("[data-upload-progress-wrap]");
    const progress = form.querySelector("[data-upload-progress]");
    const status = form.querySelector("[data-upload-status]");
    const submit = form.querySelector("[data-upload-submit]");
    const originalButtonText = submit ? submit.textContent : "Upload";
    const maxBytes = Number(form.dataset.maxUploadBytes) || 0;
    let uploading = false;

    if (!fileInput || !selection || !progressWrap || !progress || !status || !submit) return;

    fileInput.addEventListener("change", () => {
      const file = fileInput.files && fileInput.files[0];
      if (!file) {
        selection.textContent = "No file selected.";
        return;
      }
      const isZip = file.name.toLowerCase().endsWith(".zip");
      const kind = isZip ? "Album ZIP" : "Single-track audio";
      selection.textContent = `${kind}: ${file.name} · ${formatBytes(file.size)}`;
      if (maxBytes && file.size > maxBytes) {
        selection.textContent += ` · too large (limit ${formatBytes(maxBytes)})`;
      }
    });

    form.addEventListener("submit", (event) => {
      if (uploading) {
        event.preventDefault();
        return;
      }
      const file = fileInput.files && fileInput.files[0];
      if (!file) return;
      if (maxBytes && file.size > maxBytes) {
        event.preventDefault();
        status.textContent = `This file is larger than the ${formatBytes(maxBytes)} upload limit.`;
        progressWrap.hidden = false;
        return;
      }

      event.preventDefault();
      uploading = true;
      submit.disabled = true;
      submit.textContent = "Uploading…";
      progress.value = 0;
      progress.textContent = "0%";
      progressWrap.hidden = false;
      status.textContent = "Starting secure upload…";

      const request = new XMLHttpRequest();
      request.open((form.method || "POST").toUpperCase(), form.action);
      request.setRequestHeader("X-Requested-With", "FoxDenMusic");
      request.upload.addEventListener("progress", (uploadEvent) => {
        if (!uploadEvent.lengthComputable) {
          status.textContent = `Uploading ${formatBytes(uploadEvent.loaded)}…`;
          return;
        }
        const percent = Math.min(100, Math.round((uploadEvent.loaded / uploadEvent.total) * 100));
        progress.value = percent;
        progress.textContent = `${percent}%`;
        status.textContent = percent === 100
          ? "Upload sent. Waiting for the server to finish saving…"
          : `Uploading… ${percent}%`;
      });
      request.addEventListener("load", () => {
        if (request.status >= 200 && request.status < 300) {
          progress.value = 100;
          progress.textContent = "100%";
          status.textContent = "Upload received. Opening the import…";
          window.location.assign(request.responseURL || window.location.href);
          return;
        }
        uploading = false;
        submit.disabled = false;
        submit.textContent = originalButtonText;
        status.textContent = request.status === 413
          ? "Upload failed because the file is larger than the server limit."
          : `The server returned ${request.status}. Check the queue before retrying; the upload may already have been received.`;
      });
      request.addEventListener("error", () => {
        uploading = false;
        submit.disabled = false;
        submit.textContent = originalButtonText;
        status.textContent = "The connection was interrupted. Check the queue before retrying; the upload may already have been received.";
      });
      request.send(new FormData(form));
    });
  });
})();
