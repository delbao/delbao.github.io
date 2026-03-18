const POLL_INTERVAL_MS = 1500;

function parseJobInput(root) {
  const node = root.querySelector("[data-role='job-input']");
  if (!node) {
    throw new Error("Missing job input text");
  }
  return JSON.parse(node.textContent);
}

async function createJob({ apiUrl, prompt, text }) {
  const response = await fetch(apiUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      jobType: "anki_csv",
      text,
    }),
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `Failed to create job (${response.status})`);
  }

  return payload;
}

async function fetchJob(apiUrl, jobId) {
  const response = await fetch(`${apiUrl}/${encodeURIComponent(jobId)}`);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `Failed to fetch job (${response.status})`);
  }
  return payload;
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function buildDownload(root, result) {
  const download = root.querySelector("[data-role='download']");
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

  preview.hidden = false;
  preview.textContent = result.content || "";
}

async function runJob(root) {
  const apiUrl = root.dataset.llmApiUrl;
  const status = root.querySelector("[data-role='status']");
  const statusCard = root.querySelector("[data-role='status-card']");
  const trigger = root.querySelector("[data-role='trigger']");
  const download = root.querySelector("[data-role='download']");
  const preview = root.querySelector("[data-role='preview']");

  trigger.disabled = true;
  statusCard.hidden = false;
  download.hidden = true;
  preview.hidden = true;
  status.textContent = "Submitting job...";

  try {
    const text = parseJobInput(root);
    const created = await createJob({ apiUrl, text });

    status.textContent = "Job submitted. Waiting for the LLM response...";

    while (true) {
      await sleep(POLL_INTERVAL_MS);
      const job = await fetchJob(apiUrl, created.jobId);

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
    trigger.disabled = false;
  }
}

function initPostLlmTool() {
  const root = document.querySelector("[data-post-llm-tool]");
  if (!root) return;

  const trigger = root.querySelector("[data-role='trigger']");
  trigger.addEventListener("click", () => {
    runJob(root);
  });
}

initPostLlmTool();
