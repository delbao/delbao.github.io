const POLL_INTERVAL_MS = 1500;
const CREATE_JOB_TIMEOUT_MS = 8000;
const FETCH_JOB_TIMEOUT_MS = 8000;

function parseJobInput(root) {
  const node = root.querySelector("[data-role='job-input']");
  if (!node) {
    throw new Error("Missing job input text");
  }
  return JSON.parse(node.textContent);
}

function getSelectedFocuses(root) {
  return Array.from(root.querySelectorAll("[data-role='focus']:checked")).map((node) => node.dataset.focus).filter(Boolean);
}

async function fetchJsonWithTimeout(url, options, timeoutMs, fallbackMessage) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || `Request failed with status ${response.status}`);
    }
    return payload;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error(fallbackMessage);
    }
    if (error instanceof TypeError) {
      throw new Error(fallbackMessage);
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

async function createJob({ apiUrl, text, focuses }) {
  return fetchJsonWithTimeout(apiUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      jobType: "anki_csv",
      text,
      focuses,
    }),
  }, CREATE_JOB_TIMEOUT_MS, "Could not reach the API service. Make sure the backend is running on localhost:3001.");
}

async function fetchJob(apiUrl, jobId) {
  return fetchJsonWithTimeout(
    `${apiUrl}/${encodeURIComponent(jobId)}`,
    {},
    FETCH_JOB_TIMEOUT_MS,
    "Lost connection while polling the API service."
  );
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function buildDownload(root, result) {
  const download = root.querySelector("[data-role='download']");
  const copyPrompt = root.querySelector("[data-role='copy-prompt']");
  const preview = root.querySelector("[data-role='preview']");
  const blob = new Blob([result.content || ""], { type: result.contentType || "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);

  if (download.dataset.blobUrl) {
    URL.revokeObjectURL(download.dataset.blobUrl);
  }

  download.dataset.blobUrl = url;
  download.href = url;
  download.download = result.fileName || "output.txt";
  download.hidden = false;

  if (typeof result.promptText === "string" && result.promptText.trim()) {
    copyPrompt.hidden = false;
    copyPrompt.dataset.promptText = result.promptText;
  } else {
    copyPrompt.hidden = true;
    delete copyPrompt.dataset.promptText;
  }

  preview.hidden = false;
  preview.textContent = result.content || "";
}

function renderLogs(root, logs) {
  const preview = root.querySelector("[data-role='preview']");
  const lines = Array.isArray(logs) ? logs.filter((line) => typeof line === "string" && line.trim()) : [];

  if (lines.length === 0) {
    preview.hidden = true;
    preview.textContent = "";
    return;
  }

  preview.hidden = false;
  preview.textContent = lines.join("\n");
}

async function runJob(root) {
  const apiUrl = root.dataset.llmApiUrl;
  const status = root.querySelector("[data-role='status']");
  const body = root.querySelector("[data-role='body']");
  const card = root.querySelector("[data-role='status-card']");
  const submit = root.querySelector("[data-role='submit']");
  const download = root.querySelector("[data-role='download']");
  const copyPrompt = root.querySelector("[data-role='copy-prompt']");
  const preview = root.querySelector("[data-role='preview']");
  const focuses = getSelectedFocuses(root);

  card.hidden = false;
  body.hidden = false;
  submit.disabled = true;
  download.hidden = true;
  copyPrompt.hidden = true;
  delete copyPrompt.dataset.promptText;
  preview.hidden = true;
  status.textContent = "Submitting job...";

  try {
    const text = parseJobInput(root);
    const created = await createJob({ apiUrl, text, focuses });

    status.textContent = "Job submitted. Waiting for the LLM response...";
    renderLogs(root, ["Queued job"]);

    while (true) {
      await sleep(POLL_INTERVAL_MS);
      const job = await fetchJob(apiUrl, created.jobId);
      renderLogs(root, job.logs);

      if (job.status === "completed") {
        buildDownload(root, job.result);
        status.textContent = "ANKI CSV is ready.";
        break;
      }

      if (job.status === "failed") {
        throw new Error(job.error || "Job failed");
      }

      status.textContent = "Still generating cards...";
    }
  } catch (error) {
    status.textContent = error.message || "Job failed";
  } finally {
    submit.disabled = false;
  }
}

function initPostLlmTool() {
  const root = document.querySelector("[data-post-llm-tool]");
  if (!root) return;

  const submit = root.querySelector("[data-role='submit']");
  const toggle = root.querySelector("[data-role='toggle']");
  const body = root.querySelector("[data-role='body']");
  const card = root.querySelector("[data-role='status-card']");
  const copyPrompt = root.querySelector("[data-role='copy-prompt']");

  function setExpanded(expanded) {
    card.hidden = !expanded;
    body.hidden = !expanded;
    toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  }

  submit.addEventListener("click", () => {
    setExpanded(true);
    runJob(root);
  });

  copyPrompt.addEventListener("click", async () => {
    const promptText = copyPrompt.dataset.promptText || "";
    if (!promptText.trim()) {
      return;
    }

    try {
      await navigator.clipboard.writeText(promptText);
      copyPrompt.textContent = "Prompt copied";
      window.setTimeout(() => {
        copyPrompt.textContent = "Copy prompt";
      }, 1500);
    } catch (_error) {
      copyPrompt.textContent = "Copy failed";
      window.setTimeout(() => {
        copyPrompt.textContent = "Copy prompt";
      }, 1500);
    }
  });

  toggle.addEventListener("click", () => {
    setExpanded(card.hidden);
  });

  setExpanded(false);
}

initPostLlmTool();
