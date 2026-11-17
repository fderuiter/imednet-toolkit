/**
 * Sphinx-Thebe integration with Pyodide CDN runtime.
 * Provides on-demand client-side WebAssembly execution for Python code blocks.
 */
(function () {
  "use strict";

  const PYODIDE_CDN_URL = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js";

  // Event handlers for thebelab compatibility
  const listeners = {};
  function on(evt, fn) {
    if (!listeners[evt]) listeners[evt] = [];
    listeners[evt].push(fn);
  }
  function trigger(evt, data) {
    const handlers = listeners[evt] || [];
    handlers.forEach((fn) => {
      try {
        fn(evt, data);
      } catch (e) {
        console.error("[thebe-pyodide] Event handler error:", e);
      }
    });
  }

  let pyodideWorker = null;
  let pyodideReady = false;
  let pyodideInitializing = false;
  let mainThreadPyodide = null;
  let executionId = 0;
  const pendingRequests = new Map();
  const installedMainWheels = new Set();

  function getConfig() {
    const defaultWheels = ["_static/wheels/imednet-1.1.0-py3-none-any.whl"];
    const defaultRequirements = ["pydantic", "httpx", "tenacity", "defusedxml", "python-dotenv"];

    let rawConfig = null;
    if (typeof window !== "undefined" && window.thebe_config) {
      rawConfig = window.thebe_config;
    } else {
      const scriptTag = document.querySelector('script[type="text/x-thebe-config"]');
      if (scriptTag) {
        try {
          rawConfig = JSON.parse(scriptTag.textContent);
        } catch (e) {
          // ignore parsing error
        }
      }
    }

    const wheels = (rawConfig && (rawConfig.wheels || rawConfig.wheel_paths)) || defaultWheels;
    const requirements = (rawConfig && (rawConfig.requirements || rawConfig.micropip_requirements)) || defaultRequirements;

    const docRoot = (typeof DOCUMENTATION_OPTIONS !== "undefined" && DOCUMENTATION_OPTIONS.URL_ROOT)
      ? DOCUMENTATION_OPTIONS.URL_ROOT
      : "";

    const resolvedWheels = wheels.map((w) => {
      try {
        const fullRel = (w.startsWith("http://") || w.startsWith("https://"))
          ? w
          : (docRoot + w);
        return new URL(fullRel, window.location.href).href;
      } catch (e) {
        return w;
      }
    });

    return {
      wheels: resolvedWheels,
      requirements: requirements,
    };
  }

  function setCellRunButtonsDisabled(disabled) {
    document.querySelectorAll(".thebelab-run-button").forEach((btn) => {
      btn.disabled = disabled;
      btn.title = disabled
        ? "Runtime initializing, please wait..."
        : "Execute cell (Shift+Enter)";
    });
  }

  // Web Worker script running in background thread to avoid UI freeze
  const workerCode = `
    let pyodide = null;
    let ready = false;
    let initPromise = null;
    const installedWheels = new Set();

    async function initRuntime(config) {
      if (ready) return;
      importScripts("${PYODIDE_CDN_URL}");
      pyodide = await loadPyodide();

      // Load micropip
      await pyodide.loadPackage("micropip");
      const micropip = pyodide.pyimport("micropip");

      const wheels = (config && config.wheels) || [];
      const requirements = (config && config.requirements) || [];

      // Pre-install package requirements if available
      if (requirements && requirements.length > 0) {
        try {
          await micropip.install(requirements);
        } catch (reqErr) {
          console.warn("[thebe-pyodide worker] micropip requirements install warning:", reqErr);
        }
      }

      // Install SDK wheels with caching
      for (const wheelUrl of wheels) {
        if (!installedWheels.has(wheelUrl)) {
          try {
            await pyodide.runPythonAsync(\`
import micropip
try:
    await micropip.install("\${wheelUrl}")
except Exception:
    await micropip.install("\${wheelUrl}", deps=False)
\`);
            installedWheels.add(wheelUrl);
            console.log("[thebe-pyodide worker] Successfully installed wheel:", wheelUrl);
          } catch (whlErr) {
            console.error("[thebe-pyodide worker] Wheel install error:", wheelUrl, whlErr);
            throw whlErr;
          }
        }
      }

      ready = true;
      postMessage({ type: 'status', status: 'ready', message: 'Pyodide runtime initialized' });
    }

    onmessage = async function(e) {
      const data = e.data;
      if (data.type === 'init') {
        if (!initPromise) {
          initPromise = initRuntime(data.config).catch(err => {
            postMessage({ type: 'status', status: 'failed', message: err.toString() });
          });
        }
        return;
      }

      const { id, code } = data;
      if (!ready) {
        postMessage({ id, type: 'result', stdout: '', stderr: 'Pyodide runtime not ready yet.\\n', result: null });
        return;
      }

      try {
        pyodide.globals.set("__thebe_user_code__", code);
        const runnerScript = \`
import sys, ast, traceback
from io import StringIO

_stdout_buf = StringIO()
_stderr_buf = StringIO()
_old_out = sys.stdout
_old_err = sys.stderr
sys.stdout = _stdout_buf
sys.stderr = _stderr_buf
_eval_res = None
_tb_str = ""

try:
    _parsed = ast.parse(__thebe_user_code__)
    if _parsed.body and isinstance(_parsed.body[-1], ast.Expr):
        _last_expr = ast.Expression(_parsed.body.pop().value)
        _exec_code = compile(_parsed, '<stdin>', 'exec')
        _eval_code = compile(_last_expr, '<stdin>', 'eval')
        exec(_exec_code, globals())
        _eval_res = eval(_eval_code, globals())
    else:
        _exec_code = compile(__thebe_user_code__, '<stdin>', 'exec')
        exec(_exec_code, globals())
except Exception:
    _tb_str = traceback.format_exc()
finally:
    sys.stdout = _old_out
    sys.stderr = _old_err

{
    "stdout": _stdout_buf.getvalue(),
    "stderr": _stderr_buf.getvalue() + _tb_str,
    "result": repr(_eval_res) if _eval_res is not None else None
}
\`;
        const resProxy = pyodide.runPython(runnerScript);
        const resObj = resProxy.toJs();
        resProxy.destroy();

        postMessage({
          id,
          type: 'result',
          stdout: resObj.get ? resObj.get('stdout') : resObj.stdout,
          stderr: resObj.get ? resObj.get('stderr') : resObj.stderr,
          result: resObj.get ? resObj.get('result') : resObj.result
        });
      } catch (err) {
        postMessage({
          id,
          type: 'result',
          stdout: '',
          stderr: err.toString(),
          result: null
        });
      }
    };
  `;

  function initWorker() {
    if (pyodideWorker || pyodideInitializing) return;
    pyodideInitializing = true;
    setCellRunButtonsDisabled(true);
    updateLaunchButtons("loading", "Initializing Pyodide WebAssembly runtime...");

    const config = getConfig();

    try {
      const blob = new Blob([workerCode], { type: "application/javascript" });
      const workerUrl = URL.createObjectURL(blob);
      pyodideWorker = new Worker(workerUrl);

      // Trigger initialization in worker with wheel configuration
      pyodideWorker.postMessage({ type: "init", config: config });

      pyodideWorker.onmessage = function (e) {
        const data = e.data;
        if (data.type === "status") {
          if (data.status === "ready") {
            pyodideReady = true;
            pyodideInitializing = false;
            setCellRunButtonsDisabled(false);
            updateLaunchButtons("ready", "Pyodide Ready");
            trigger("status", { status: "ready", message: data.message });
          } else {
            pyodideInitializing = false;
            updateLaunchButtons("failed", "Initialization failed");
            trigger("status", { status: "failed", message: data.message });
          }
        } else if (data.type === "result") {
          const cb = pendingRequests.get(data.id);
          if (cb) {
            pendingRequests.delete(data.id);
            cb(data);
          }
        }
      };

      pyodideWorker.onerror = function (err) {
        console.warn("[thebe-pyodide] Worker error, falling back to main-thread Pyodide:", err);
        fallbackToMainThread();
      };
    } catch (e) {
      console.warn("[thebe-pyodide] Worker creation failed, falling back to main-thread Pyodide:", e);
      fallbackToMainThread();
    }
  }

  function fallbackToMainThread() {
    if (pyodideReady) return;
    setCellRunButtonsDisabled(true);
    const config = getConfig();
    const script = document.createElement("script");
    script.src = PYODIDE_CDN_URL;
    script.onload = async () => {
      try {
        mainThreadPyodide = await window.loadPyodide();
        await mainThreadPyodide.loadPackage("micropip");
        const micropip = mainThreadPyodide.pyimport("micropip");

        if (config.requirements && config.requirements.length > 0) {
          try {
            await micropip.install(config.requirements);
          } catch (reqErr) {
            console.warn("[thebe-pyodide] main thread micropip requirements warning:", reqErr);
          }
        }

        for (const wheelUrl of config.wheels) {
          if (!installedMainWheels.has(wheelUrl)) {
            try {
              await mainThreadPyodide.runPythonAsync(`
import micropip
try:
    await micropip.install("${wheelUrl}")
except Exception:
    await micropip.install("${wheelUrl}", deps=False)
`);
              installedMainWheels.add(wheelUrl);
            } catch (whlErr) {
              console.error("[thebe-pyodide] main thread failed installing wheel:", wheelUrl, whlErr);
              throw whlErr;
            }
          }
        }

        pyodideReady = true;
        pyodideInitializing = false;
        setCellRunButtonsDisabled(false);
        updateLaunchButtons("ready", "Pyodide Ready");
        trigger("status", { status: "ready", message: "Pyodide runtime initialized (main thread)" });
      } catch (err) {
        pyodideInitializing = false;
        updateLaunchButtons("failed", "Failed to load Pyodide");
        trigger("status", { status: "failed", message: err.toString() });
      }
    };
    script.onerror = (err) => {
      pyodideInitializing = false;
      updateLaunchButtons("failed", "Failed to load Pyodide script");
      trigger("status", { status: "failed", message: "CDN script load error" });
    };
    document.head.appendChild(script);
  }

  function executeCode(code, callback) {
    if (!pyodideReady) {
      callback({
        stdout: "",
        stderr: "Pyodide runtime is still initializing. Please wait a moment...",
        result: null,
      });
      return;
    }

    if (pyodideWorker) {
      const id = ++executionId;
      pendingRequests.set(id, callback);
      pyodideWorker.postMessage({ id, code });
    } else if (mainThreadPyodide) {
      try {
        mainThreadPyodide.globals.set("__thebe_user_code__", code);
        const runnerScript = `
import sys, ast, traceback
from io import StringIO

_stdout_buf = StringIO()
_stderr_buf = StringIO()
_old_out = sys.stdout
_old_err = sys.stderr
sys.stdout = _stdout_buf
sys.stderr = _stderr_buf
_eval_res = None
_tb_str = ""

try:
    _parsed = ast.parse(__thebe_user_code__)
    if _parsed.body and isinstance(_parsed.body[-1], ast.Expr):
        _last_expr = ast.Expression(_parsed.body.pop().value)
        _exec_code = compile(_parsed, '<stdin>', 'exec')
        _eval_code = compile(_last_expr, '<stdin>', 'eval')
        exec(_exec_code, globals())
        _eval_res = eval(_eval_code, globals())
    else:
        _exec_code = compile(__thebe_user_code__, '<stdin>', 'exec')
        exec(_exec_code, globals())
except Exception:
    _tb_str = traceback.format_exc()
finally:
    sys.stdout = _old_out
    sys.stderr = _old_err

{
    "stdout": _stdout_buf.getvalue(),
    "stderr": _stderr_buf.getvalue() + _tb_str,
    "result": repr(_eval_res) if _eval_res is not None else None
}
`;
        const resProxy = mainThreadPyodide.runPython(runnerScript);
        const resObj = resProxy.toJs();
        resProxy.destroy();
        callback({
          stdout: resObj.get ? resObj.get("stdout") : resObj.stdout,
          stderr: resObj.get ? resObj.get("stderr") : resObj.stderr,
          result: resObj.get ? resObj.get("result") : resObj.result,
        });
      } catch (err) {
        callback({
          stdout: "",
          stderr: err.toString(),
          result: null,
        });
      }
    }
  }

  function updateLaunchButtons(status, text) {
    document.querySelectorAll(".thebe-launch-button").forEach((btn) => {
      btn.classList.remove("thebe-status-loading", "thebe-status-building", "thebe-status-ready", "thebe-status-failed");
      btn.classList.add(`thebe-status-${status}`);

      const loadingText = btn.querySelector(".loading-text");
      if (loadingText) {
        loadingText.innerHTML = `<span class="launch_msg">Pyodide (WebAssembly): </span><span class="status">${status}</span>`;
      } else {
        btn.textContent = text;
      }
    });
  }

  function transformCodeCells() {
    const selector = (typeof thebe_selector !== "undefined" && thebe_selector)
      ? thebe_selector
      : "div.highlight-python";

    const codeBlocks = document.querySelectorAll(selector);
    codeBlocks.forEach((block, index) => {
      if (block.classList.contains("thebelab-cell")) return;
      block.classList.add("thebelab-cell", "thebe-cell");
      block.id = `thebe-codecell-${index}`;

      const pre = block.querySelector("pre");
      if (!pre) return;

      const originalCode = pre.innerText.trim();

      // Create editor
      const editorWrapper = document.createElement("div");
      editorWrapper.className = "thebelab-input";

      const textarea = document.createElement("textarea");
      textarea.className = "thebe-editor";
      textarea.value = originalCode;
      textarea.spellcheck = false;
      textarea.setAttribute("aria-label", "Python interactive code cell");

      // Auto-height adjustment
      const lineCount = (originalCode.match(/\n/g) || []).length + 1;
      textarea.rows = Math.max(3, Math.min(lineCount + 1, 25));

      editorWrapper.appendChild(textarea);

      // Create toolbar controls
      const controls = document.createElement("div");
      controls.className = "thebelab-controls";

      const runBtn = document.createElement("button");
      runBtn.type = "button";
      runBtn.className = "thebelab-button thebelab-run-button";
      runBtn.innerHTML = "&#9654; Run";
      runBtn.disabled = !pyodideReady;
      runBtn.title = pyodideReady ? "Execute cell (Shift+Enter)" : "Runtime initializing, please wait...";

      const clearBtn = document.createElement("button");
      clearBtn.type = "button";
      clearBtn.className = "thebelab-button thebelab-clear-button";
      clearBtn.textContent = "Clear";
      clearBtn.title = "Clear cell output";

      const cellStatus = document.createElement("span");
      cellStatus.className = "thebelab-cell-status";
      cellStatus.setAttribute("role", "status");
      cellStatus.setAttribute("aria-live", "polite");
      cellStatus.setAttribute("aria-atomic", "true");

      controls.appendChild(runBtn);
      controls.appendChild(clearBtn);
      controls.appendChild(cellStatus);

      // Create output container
      const outputArea = document.createElement("div");
      outputArea.className = "jp-OutputArea thebelab-output";
      const outputContent = document.createElement("div");
      outputContent.className = "thebe-output-area";
      outputArea.appendChild(outputContent);

      // Hide original pre and append new interactive elements
      pre.style.display = "none";
      block.appendChild(editorWrapper);
      block.appendChild(controls);
      block.appendChild(outputArea);

      // Execution handler
      function runCurrentCell() {
        if (!pyodideReady) return;
        const code = textarea.value;
        runBtn.disabled = true;
        runBtn.innerHTML = '<span class="thebe-spin">&#8635;</span> Running...';
        cellStatus.textContent = "Executing...";

        executeCode(code, (res) => {
          runBtn.disabled = false;
          runBtn.innerHTML = "&#9654; Run";
          if (res.stderr && res.stderr.trim() && !res.stdout && !res.result) {
            cellStatus.textContent = "Execution completed with errors.";
          } else {
            cellStatus.textContent = "Execution completed.";
          }

          outputContent.innerHTML = "";

          let hasOutput = false;
          if (res.stdout && res.stdout.trim()) {
            hasOutput = true;
            const outPre = document.createElement("pre");
            outPre.className = "thebe-stdout";
            outPre.textContent = res.stdout;
            outputContent.appendChild(outPre);
          }

          if (res.stderr && res.stderr.trim()) {
            hasOutput = true;
            const errPre = document.createElement("pre");
            errPre.className = "thebe-stderr";
            errPre.style.color = "#cf222e";
            errPre.textContent = res.stderr;
            outputContent.appendChild(errPre);
          }

          if (res.result && res.result !== "None") {
            hasOutput = true;
            const resPre = document.createElement("pre");
            resPre.className = "thebe-result";
            resPre.textContent = res.result;
            outputContent.appendChild(resPre);
          }

          if (!hasOutput) {
            const emptyNotice = document.createElement("div");
            emptyNotice.className = "thebe-no-output";
            emptyNotice.textContent = "<Execution finished with no output>";
            outputContent.appendChild(emptyNotice);
          }
        });
      }

      runBtn.addEventListener("click", runCurrentCell);

      clearBtn.addEventListener("click", () => {
        outputContent.innerHTML = "";
        cellStatus.textContent = "";
      });

      // Shift+Enter / Ctrl+Enter shortcut
      textarea.addEventListener("keydown", (e) => {
        if ((e.shiftKey || e.ctrlKey) && e.key === "Enter") {
          e.preventDefault();
          if (pyodideReady) {
            runCurrentCell();
          }
        }
      });
    });
  }

  function bootstrap() {
    initWorker();
    transformCodeCells();
  }

  // Define global thebelab object for sphinx-thebe compatibility
  window.thebelab = {
    on: on,
    trigger: trigger,
    bootstrap: bootstrap,
    status: () => (pyodideReady ? "ready" : pyodideInitializing ? "loading" : "idle"),
  };

  // Provide or wrap initThebe
  window.initThebe = function () {
    console.log("[thebe-pyodide]: Initializing Thebe with Pyodide runtime...");
    if (typeof configureThebe === "function") {
      try {
        configureThebe();
      } catch (e) {
        console.warn("[thebe-pyodide] configureThebe warning:", e);
      }
    }
    bootstrap();
  };

  // Auto-listen for DOM ready to bind clicks if necessary
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".thebe-launch-button").forEach((btn) => {
      if (!btn.getAttribute("onclick")) {
        btn.addEventListener("click", window.initThebe);
      }
    });
  });
})();
