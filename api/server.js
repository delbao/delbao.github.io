const express = require("express");
const cors = require("cors");
const dotenv = require("dotenv");
const Searchkit = require("@searchkit/api").default;
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

dotenv.config();

const port = Number(process.env.PORT || 3001);
const indexName = process.env.SEARCHKIT_INDEX || "private_posts";
const jobRetentionMs = Number(process.env.LLM_JOB_RETENTION_MS || 3600000);
const pythonExecutable = (() => {
  const repoVenvPython = path.resolve(__dirname, "..", ".venv", "bin", "python");
  return process.env.PYTHON_EXECUTABLE || (fs.existsSync(repoVenvPython) ? repoVenvPython : "python3");
})();
const shouldForceArm64Python =
  process.platform === "darwin" &&
  process.arch === "x64" &&
  fs.existsSync("/usr/bin/arch") &&
  fs.existsSync(path.resolve(__dirname, "..", ".venv", "bin", "python"));
const pythonJobScript = path.resolve(__dirname, "..", "preprocessing", "anki_job.py");
const pythonJobModel = process.env.ANKI_JOB_MODEL || "gpt-4.1-mini";
const pythonJobTimeoutSeconds = Number(process.env.ANKI_JOB_TIMEOUT_SECONDS || 120);
const pythonJobRetries = Number(process.env.ANKI_JOB_RETRIES || 2);
const pythonJobPromptName = process.env.ANKI_JOB_PROMPT_NAME || "anki_cards";

const auth = (() => {
  if (process.env.ELASTICSEARCH_API_KEY) {
    return { apiKey: process.env.ELASTICSEARCH_API_KEY };
  }
  if (process.env.ELASTICSEARCH_USERNAME && process.env.ELASTICSEARCH_PASSWORD) {
    return {
      username: process.env.ELASTICSEARCH_USERNAME,
      password: process.env.ELASTICSEARCH_PASSWORD,
    };
  }
  return undefined;
})();

const client = Searchkit(
  {
    connection: {
      host: process.env.ELASTICSEARCH_URL || "http://localhost:9200",
      auth,
    },
    search_settings: {
      search_attributes: [{ field: "title", weight: 5 }, { field: "content", weight: 1 }, "tags", "categories"],
      result_attributes: ["id", "title", "url", "date", "content", "tags", "categories"],
      highlight_attributes: ["title", "content"],
    },
  },
  {
    debug: process.env.SEARCHKIT_DEBUG === "1",
  }
);

const app = express();
const jobs = new Map();
const allowedOrigins = (process.env.SEARCH_ALLOWED_ORIGINS || "")
  .split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);

app.use(express.json({ limit: "1mb" }));
app.use(
  cors(
    allowedOrigins.length > 0
      ? {
          origin(origin, callback) {
            if (!origin || allowedOrigins.includes(origin)) {
              callback(null, true);
              return;
            }
            callback(new Error(`Origin not allowed: ${origin}`));
          },
        }
      : undefined
  )
);

app.get("/health", (_req, res) => {
  res.json({
    ok: true,
    indexName,
    pythonExecutable,
    pythonJobScript,
  });
});

function cleanupJobs() {
  const now = Date.now();
  for (const [jobId, job] of jobs.entries()) {
    if (now - job.updatedAt > jobRetentionMs) {
      jobs.delete(jobId);
    }
  }
}

function appendJobLog(job, message) {
  if (!job || typeof message !== "string") {
    return;
  }

  const trimmed = message.trim();
  if (!trimmed) {
    return;
  }

  if (!Array.isArray(job.logs)) {
    job.logs = [];
  }

  job.logs.push(trimmed);
  if (job.logs.length > 200) {
    job.logs = job.logs.slice(-200);
  }
  job.updatedAt = Date.now();
}

function slugifyFilePart(value) {
  return String(value || "anki-cards")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48) || "anki-cards";
}

function deriveFileStemFromText(text) {
  if (typeof text !== "string") {
    return "anki-cards";
  }

  const titleLine = text
    .split("\n")
    .map((line) => line.trim())
    .find((line) => line.toLowerCase().startsWith("title:"));

  if (titleLine) {
    return slugifyFilePart(titleLine.slice("title:".length).trim());
  }

  return slugifyFilePart(text.slice(0, 80));
}

function runPythonJob(job) {
  return new Promise((resolve, reject) => {
    const pythonArgs = [
      pythonJobScript,
      "--model",
      pythonJobModel,
      "--timeout-seconds",
      String(pythonJobTimeoutSeconds),
      "--retries",
      String(pythonJobRetries),
      "--prompt-name",
      pythonJobPromptName,
    ];

    const command = shouldForceArm64Python ? "/usr/bin/arch" : pythonExecutable;
    const args = shouldForceArm64Python ? ["-arm64", pythonExecutable, ...pythonArgs] : pythonArgs;

    const child = spawn(command, args, {
      cwd: path.resolve(__dirname, ".."),
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";
    let stderrBuffer = "";

    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });

    child.stderr.on("data", (chunk) => {
      const text = chunk.toString();
      stderr += text;
      stderrBuffer += text;

      const lines = stderrBuffer.split(/\r?\n/);
      stderrBuffer = lines.pop() || "";
      lines.forEach((line) => appendJobLog(job, line));
    });

    child.on("error", (error) => {
      reject(error);
    });

    child.on("close", (code) => {
      appendJobLog(job, stderrBuffer);
      if (code !== 0) {
        reject(new Error(stderr.trim() || `Python job exited with code ${code}`));
        return;
      }

      try {
        resolve(JSON.parse(stdout));
      } catch (error) {
        reject(new Error(`Python job returned invalid JSON: ${error.message}`));
      }
    });

    child.stdin.write(
      JSON.stringify({
        job_type: job.jobType,
        text: job.text,
        file_stem: job.fileStem,
        prompt_name: job.promptName,
        mode: job.mode,
        point_count: job.pointCount,
        user_pointers: job.userPointers,
        focuses: job.focuses,
      })
    );
    child.stdin.end();
  });
}

async function executeJob(jobId) {
  const job = jobs.get(jobId);
  if (!job) {
    return;
  }

  job.status = "running";
  job.updatedAt = Date.now();
  appendJobLog(job, `Starting ${job.jobType} job`);

  try {
    if (job.jobType !== "anki_csv") {
      throw new Error(`Unsupported job type: ${job.jobType}`);
    }

    const result = await runPythonJob(job);
    if (!result || typeof result.content !== "string" || !result.content.trim()) {
      throw new Error("Python job returned no CSV content");
    }

    appendJobLog(job, "Job finished successfully");
    job.status = "completed";
    job.result = {
      content: result.content,
      contentType: result.content_type || "text/csv;charset=utf-8",
      fileName: result.file_name || `${job.fileStem || "anki-cards"}-anki.csv`,
      promptText: result.prompt_text || "",
    };
  } catch (error) {
    appendJobLog(job, error instanceof Error ? error.message : "Unknown job failure");
    job.status = "failed";
    job.error = error instanceof Error ? error.message : "Unknown job failure";
  } finally {
    job.updatedAt = Date.now();
  }
}

app.post("/api/search", async (req, res) => {
  try {
    const requestBody = Array.isArray(req.body) ? req.body : req.body && req.body.requests;
    if (!Array.isArray(requestBody)) {
      res.status(400).json({ error: "Expected request body to contain an InstantSearch request array" });
      return;
    }
    const results = await client.handleRequest(requestBody);
    res.json(results);
  } catch (error) {
    console.error("Search request failed", error);
    res.status(500).json({ error: "Search request failed" });
  }
});

app.post("/api/llm-jobs", (req, res) => {
  cleanupJobs();

  const { jobType, text = "", mode = "", pointCount = 5, userPointers = "", focuses = [] } = req.body || {};
  if (jobType !== "anki_csv") {
    res.status(400).json({ error: "Only jobType=anki_csv is currently supported" });
    return;
  }

  if (typeof text !== "string" || !text.trim()) {
    res.status(400).json({ error: "A non-empty text payload is required" });
    return;
  }

  const jobId = crypto.randomUUID();
  const job = {
    id: jobId,
    jobType,
    text,
    fileStem: deriveFileStemFromText(text),
    promptName: pythonJobPromptName,
    mode: typeof mode === "string" ? mode.trim() : "",
    pointCount: Number.isFinite(Number(pointCount)) && Number(pointCount) > 0 ? Math.max(1, Math.min(50, Math.trunc(Number(pointCount)))) : 5,
    userPointers: typeof userPointers === "string" ? userPointers.trim() : "",
    focuses: Array.isArray(focuses) ? focuses.filter((item) => typeof item === "string" && item.trim()) : [],
    status: "queued",
    logs: ["Queued job"],
    createdAt: Date.now(),
    updatedAt: Date.now(),
  };

  jobs.set(jobId, job);
  void executeJob(jobId);

  res.status(202).json({
    jobId,
    status: job.status,
  });
});

app.get("/api/llm-jobs/:jobId", (req, res) => {
  cleanupJobs();

  const job = jobs.get(req.params.jobId);
  if (!job) {
    res.status(404).json({ error: "Job not found" });
    return;
  }

  res.json({
    jobId: job.id,
    jobType: job.jobType,
    status: job.status,
    logs: Array.isArray(job.logs) ? job.logs : [],
    error: job.error,
    result: job.status === "completed" ? job.result : undefined,
    createdAt: job.createdAt,
    updatedAt: job.updatedAt,
  });
});

app.listen(port, () => {
  console.log(`API service listening on http://localhost:${port} (index: ${indexName})`);
});
