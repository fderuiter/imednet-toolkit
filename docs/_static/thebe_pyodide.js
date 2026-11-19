/**
 * Native Sphinx Pyodide client runtime.
 * Provides on-demand client-side WebAssembly execution for Python code blocks.
 */
(function () {
  "use strict";

  const PYODIDE_CDN_URL = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js";

  const EXCLUDED_WHEEL_PREFIXES = [
    "apache_airflow_providers_imednet-",
    "apache-airflow-providers-imednet-",
    "imednet_tui-",
    "imednet-tui-",
    "imednet_workspace-",
    "imednet-workspace-",
  ];

  // Event handlers for runtime status notifications
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
        console.error("[pyodide-cells] Event handler error:", e);
      }
    });
  }

  let pyodideWorker = null;
  let pyodideReady = false;
  let pyodideInitializing = false;
  let mainThreadPyodide = null;
  let mainExecQueue = Promise.resolve();
  let executionId = 0;
  const pendingRequests = new Map();
  const installedMainWheels = new Set();

  function isAllowedBrowserWheel(wheelPath) {
    const filename = String(wheelPath || "").split("/").pop() || "";
    return !EXCLUDED_WHEEL_PREFIXES.some((prefix) => filename.startsWith(prefix));
  }

  function normalizeRootPrefix(prefix) {
    if (!prefix) return "";
    return prefix.endsWith("/") ? prefix : `${prefix}/`;
  }

  function getConfig() {
    const defaultWheels = [
      "_static/wheels/imednet-1.1.0-py3-none-any.whl",
      "_static/wheels/imednet_workflows-1.1.0-py3-none-any.whl",
      "_static/wheels/imednet_plugins_sinks-1.0.1-py3-none-any.whl",
    ];
    const defaultPyodidePackages = [
      "micropip",
      "pydantic",
      "cryptography",
      "pandas",
      "ssl",
      "sqlite3",
    ];
    const defaultRequirements = [
      "httpx",
      "tenacity",
      "defusedxml",
      "python-dotenv",
      "filelock",
      "python-json-logger",
    ];
    const defaultEnv = {
      IMEDNET_SIMULATION_MODE: "true",
      IMEDNET_API_KEY: "dummy_api_key", // pragma: allowlist secret
      IMEDNET_SECURITY_KEY: "dummy_security_key", // pragma: allowlist secret
      SNOWFLAKE_PASSWORD: "dummy_password", // pragma: allowlist secret
      IMEDNET_STUDY_KEY: "MY_STUDY",
    };

    let rawConfig = null;
    if (typeof document !== "undefined") {
      const jsonConfigScript = document.getElementById("imednet-pyodide-config");
      if (jsonConfigScript && jsonConfigScript.textContent) {
        try {
          rawConfig = JSON.parse(jsonConfigScript.textContent);
        } catch (e) {
          console.warn("[pyodide-cells] Failed to parse #imednet-pyodide-config:", e);
        }
      }
    }

    if (
      !rawConfig &&
      typeof window !== "undefined" &&
      window.pyodide_config &&
      typeof window.pyodide_config === "object"
    ) {
      rawConfig = window.pyodide_config;
    } else if (
      !rawConfig &&
      typeof window !== "undefined" &&
      window.thebe_config &&
      typeof window.thebe_config === "object" &&
      !(typeof Element !== "undefined" && window.thebe_config instanceof Element)
    ) {
      rawConfig = window.thebe_config;
    }

    const rawWheels =
      (rawConfig && (rawConfig.wheels || rawConfig.wheel_paths)) || defaultWheels;
    const wheels = rawWheels.filter(isAllowedBrowserWheel);
    const pyodidePackages =
      (rawConfig && rawConfig.pyodide_packages) || defaultPyodidePackages;
    const requirements =
      (rawConfig && (rawConfig.requirements || rawConfig.micropip_requirements)) ||
      defaultRequirements;
    const env = Object.assign({}, defaultEnv, (rawConfig && rawConfig.env) || {});
    const selector = (rawConfig && rawConfig.selector) || "div.highlight-python";

    let docRoot = "";
    const htmlEl = typeof document !== "undefined" ? document.documentElement : null;
    const contentRootAttr =
      htmlEl && typeof htmlEl.getAttribute === "function"
        ? htmlEl.getAttribute("data-content_root")
        : null;

    if (contentRootAttr !== null && contentRootAttr !== "") {
      docRoot = normalizeRootPrefix(contentRootAttr);
    } else if (
      typeof DOCUMENTATION_OPTIONS !== "undefined" &&
      DOCUMENTATION_OPTIONS &&
      DOCUMENTATION_OPTIONS.URL_ROOT
    ) {
      docRoot = normalizeRootPrefix(String(DOCUMENTATION_OPTIONS.URL_ROOT));
    }

    const baseHref =
      typeof window !== "undefined" && window.location && window.location.href
        ? window.location.href
        : "http://localhost/";

    const resolvedWheels = wheels.map((w) => {
      try {
        if (w.startsWith("http://") || w.startsWith("https://") || w.startsWith("/")) {
          return new URL(w, baseHref).href;
        }
        const cleanedRel = w.replace(/^\.\//, "");
        return new URL(docRoot + cleanedRel, baseHref).href;
      } catch (e) {
        return w;
      }
    });

    return {
      selector: selector,
      wheels: resolvedWheels,
      pyodidePackages: pyodidePackages,
      requirements: requirements,
      env: env,
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
    let execQueue = Promise.resolve();
    const installedWheels = new Set();

    async function initRuntime(config) {
      if (ready) return;
      importScripts("${PYODIDE_CDN_URL}");
      pyodide = await loadPyodide();

      const pyodidePackages = (config && config.pyodidePackages) || [
        "micropip",
        "pydantic",
        "cryptography",
        "pandas",
        "ssl",
        "sqlite3"
      ];
      await pyodide.loadPackage(pyodidePackages);

      const envVars = (config && config.env) || {};
      const envProxy = pyodide.toPy(envVars);
      pyodide.globals.set("__pyodide_init_env__", envProxy);
      await pyodide.runPythonAsync(\`
import os
for _env_k, _env_v in __pyodide_init_env__.items():
    os.environ[str(_env_k)] = str(_env_v)
del __pyodide_init_env__
\`);
      envProxy.destroy();

      const wheels = (config && config.wheels) || [];
      const requirements = (config && config.requirements) || [];

      if (requirements && requirements.length > 0) {
        const reqsProxy = pyodide.toPy(requirements);
        pyodide.globals.set("__pyodide_init_reqs__", reqsProxy);
        try {
          await pyodide.runPythonAsync(\`
import micropip
if "typing-extensions" in micropip.list():
    micropip.uninstall("typing-extensions")
_reqs = list(dict.fromkeys(["typing-extensions", *[str(_r) for _r in __pyodide_init_reqs__]]))
await micropip.install(_reqs)
\`);
        } catch (reqErr) {
          console.warn("[pyodide-cells worker] micropip requirements warning:", reqErr);
          throw reqErr;
        } finally {
          pyodide.globals.delete("__pyodide_init_reqs__");
          reqsProxy.destroy();
        }
      }

      for (const wheelUrl of wheels) {
        if (!installedWheels.has(wheelUrl)) {
          try {
            await pyodide.runPythonAsync(\`
import micropip
await micropip.install("\${wheelUrl}", deps=False)
\`);
            installedWheels.add(wheelUrl);
            console.log("[pyodide-cells worker] Installed wheel:", wheelUrl);
          } catch (whlErr) {
            console.error("[pyodide-cells worker] Wheel install error:", wheelUrl, whlErr);
            throw whlErr;
          }
        }
      }

      ready = true;
      postMessage({ type: 'status', status: 'ready', message: 'Pyodide runtime initialized' });
    }

    async function executeCellRequest(id, code) {
      if (!ready) {
        postMessage({ id, type: 'result', stdout: '', stderr: 'Pyodide runtime not ready yet.\\n', result: null, error: true });
        return;
      }

      try {
        pyodide.globals.set("__thebe_user_code__", code);
        const runnerScript = \`
import ast
import inspect
import sys
import traceback
from io import StringIO

async def _pyodide_bridge_asyncio_run(_coro, *, debug=None, loop_factory=None):
    return await _coro

class _PyodideAsyncioRunRewriter(ast.NodeTransformer):
    def visit_FunctionDef(self, node):
        return node

    def visit_AsyncFunctionDef(self, node):
        return node

    def visit_ClassDef(self, node):
        return node

    def visit_Lambda(self, node):
        return node

    def visit_Call(self, node):
        node = self.generic_visit(node)
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
            and node.func.attr == "run"
        ):
            return ast.copy_location(
                ast.Await(
                    value=ast.Call(
                        func=ast.Name(id="_pyodide_bridge_asyncio_run", ctx=ast.Load()),
                        args=node.args,
                        keywords=node.keywords,
                    )
                ),
                node,
            )
        return node

_stdout_buf = StringIO()
_stderr_buf = StringIO()
_old_out = sys.stdout
_old_err = sys.stderr
sys.stdout = _stdout_buf
sys.stderr = _stderr_buf
_eval_res = None
_tb_str = ""

try:
    globals().setdefault("__name__", "__main__")
    _parsed = ast.parse(__thebe_user_code__)
    _parsed = _PyodideAsyncioRunRewriter().visit(_parsed)
    ast.fix_missing_locations(_parsed)
    if _parsed.body and isinstance(_parsed.body[-1], ast.Expr):
        _last_expr = ast.Expression(_parsed.body.pop().value)
        ast.fix_missing_locations(_last_expr)
        _exec_code = compile(_parsed, "<stdin>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        _eval_code = compile(_last_expr, "<stdin>", "eval", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        _exec_ret = eval(_exec_code, globals())
        if inspect.iscoroutine(_exec_ret):
            await _exec_ret
        _eval_ret = eval(_eval_code, globals())
        _eval_res = (await _eval_ret) if inspect.iscoroutine(_eval_ret) else _eval_ret
    else:
        _exec_code = compile(_parsed, "<stdin>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        _exec_ret = eval(_exec_code, globals())
        if inspect.iscoroutine(_exec_ret):
            await _exec_ret
except (Exception, SystemExit) as _exc:
    if not isinstance(_exc, SystemExit) or _exc.code not in (None, 0):
        _tb_str = traceback.format_exc()
finally:
    sys.stdout = _old_out
    sys.stderr = _old_err

{
    "stdout": _stdout_buf.getvalue(),
    "stderr": _stderr_buf.getvalue() + _tb_str,
    "result": repr(_eval_res) if _eval_res is not None else None,
    "error": bool(_tb_str)
}
\`;
        const resProxy = await pyodide.runPythonAsync(runnerScript);
        const resObj = resProxy.toJs();
        resProxy.destroy();

        postMessage({
          id,
          type: 'result',
          stdout: resObj.get ? resObj.get('stdout') : resObj.stdout,
          stderr: resObj.get ? resObj.get('stderr') : resObj.stderr,
          result: resObj.get ? resObj.get('result') : resObj.result,
          error: resObj.get ? Boolean(resObj.get('error')) : Boolean(resObj.error)
        });
      } catch (err) {
        postMessage({
          id,
          type: 'result',
          stdout: '',
          stderr: err.toString(),
          result: null,
          error: true
        });
      }
    }

    onmessage = function(e) {
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
      execQueue = execQueue.then(() => executeCellRequest(id, code));
    };
  `;

  function initWorker() {
    if (pyodideWorker || pyodideInitializing || pyodideReady) return;
    pyodideInitializing = true;
    setCellRunButtonsDisabled(true);
    updateLaunchButtons("loading", "Pyodide (WebAssembly): loading");

    const config = getConfig();

    try {
      const blob = new Blob([workerCode], { type: "application/javascript" });
      const workerUrl = URL.createObjectURL(blob);
      pyodideWorker = new Worker(workerUrl);

      pyodideWorker.postMessage({ type: "init", config: config });

      pyodideWorker.onmessage = function (e) {
        const data = e.data;
        if (data.type === "status") {
          if (data.status === "ready") {
            pyodideReady = true;
            pyodideInitializing = false;
            setCellRunButtonsDisabled(false);
            updateLaunchButtons("ready", "Pyodide (WebAssembly): ready");
            trigger("status", { status: "ready", message: data.message });
          } else {
            pyodideInitializing = false;
            updateLaunchButtons("failed", "Pyodide (WebAssembly): failed");
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
        console.warn("[pyodide-cells] Worker error, falling back to main-thread Pyodide:", err);
        pyodideWorker = null;
        fallbackToMainThread();
      };
    } catch (e) {
      console.warn("[pyodide-cells] Worker creation failed, falling back to main-thread Pyodide:", e);
      pyodideWorker = null;
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
        const pyodidePackages = config.pyodidePackages || [
          "micropip",
          "pydantic",
          "cryptography",
          "pandas",
          "ssl",
          "sqlite3",
        ];
        await mainThreadPyodide.loadPackage(pyodidePackages);

        const envProxy = mainThreadPyodide.toPy(config.env || {});
        mainThreadPyodide.globals.set("__pyodide_init_env__", envProxy);
        await mainThreadPyodide.runPythonAsync(`
import os
for _env_k, _env_v in __pyodide_init_env__.items():
    os.environ[str(_env_k)] = str(_env_v)
del __pyodide_init_env__
`);
        envProxy.destroy();

        if (config.requirements && config.requirements.length > 0) {
          const reqsProxy = mainThreadPyodide.toPy(config.requirements);
          mainThreadPyodide.globals.set("__pyodide_init_reqs__", reqsProxy);
          try {
            await mainThreadPyodide.runPythonAsync(`
import micropip
if "typing-extensions" in micropip.list():
    micropip.uninstall("typing-extensions")
_reqs = list(dict.fromkeys(["typing-extensions", *[str(_r) for _r in __pyodide_init_reqs__]]))
await micropip.install(_reqs)
`);
          } catch (reqErr) {
            console.warn("[pyodide-cells] main thread micropip requirements warning:", reqErr);
            throw reqErr;
          } finally {
            mainThreadPyodide.globals.delete("__pyodide_init_reqs__");
            reqsProxy.destroy();
          }
        }

        for (const wheelUrl of config.wheels) {
          if (!installedMainWheels.has(wheelUrl)) {
            try {
              await mainThreadPyodide.runPythonAsync(`
import micropip
await micropip.install("${wheelUrl}", deps=False)
`);
              installedMainWheels.add(wheelUrl);
            } catch (whlErr) {
              console.error("[pyodide-cells] main thread failed installing wheel:", wheelUrl, whlErr);
              throw whlErr;
            }
          }
        }

        pyodideReady = true;
        pyodideInitializing = false;
        setCellRunButtonsDisabled(false);
        updateLaunchButtons("ready", "Pyodide (WebAssembly): ready");
        trigger("status", { status: "ready", message: "Pyodide runtime initialized (main thread)" });
      } catch (err) {
        pyodideInitializing = false;
        updateLaunchButtons("failed", "Pyodide (WebAssembly): failed");
        trigger("status", { status: "failed", message: err.toString() });
      }
    };
    script.onerror = () => {
      pyodideInitializing = false;
      updateLaunchButtons("failed", "Pyodide (WebAssembly): failed");
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
        error: true,
      });
      return;
    }

    if (pyodideWorker) {
      const id = ++executionId;
      pendingRequests.set(id, callback);
      pyodideWorker.postMessage({ id, code });
    } else if (mainThreadPyodide) {
      mainExecQueue = mainExecQueue.then(async () => {
        try {
          mainThreadPyodide.globals.set("__thebe_user_code__", code);
          const runnerScript = `
import ast
import inspect
import sys
import traceback
from io import StringIO

async def _pyodide_bridge_asyncio_run(_coro, *, debug=None, loop_factory=None):
    return await _coro

class _PyodideAsyncioRunRewriter(ast.NodeTransformer):
    def visit_FunctionDef(self, node):
        return node

    def visit_AsyncFunctionDef(self, node):
        return node

    def visit_ClassDef(self, node):
        return node

    def visit_Lambda(self, node):
        return node

    def visit_Call(self, node):
        node = self.generic_visit(node)
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
            and node.func.attr == "run"
        ):
            return ast.copy_location(
                ast.Await(
                    value=ast.Call(
                        func=ast.Name(id="_pyodide_bridge_asyncio_run", ctx=ast.Load()),
                        args=node.args,
                        keywords=node.keywords,
                    )
                ),
                node,
            )
        return node

_stdout_buf = StringIO()
_stderr_buf = StringIO()
_old_out = sys.stdout
_old_err = sys.stderr
sys.stdout = _stdout_buf
sys.stderr = _stderr_buf
_eval_res = None
_tb_str = ""

try:
    globals().setdefault("__name__", "__main__")
    _parsed = ast.parse(__thebe_user_code__)
    _parsed = _PyodideAsyncioRunRewriter().visit(_parsed)
    ast.fix_missing_locations(_parsed)
    if _parsed.body and isinstance(_parsed.body[-1], ast.Expr):
        _last_expr = ast.Expression(_parsed.body.pop().value)
        ast.fix_missing_locations(_last_expr)
        _exec_code = compile(_parsed, "<stdin>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        _eval_code = compile(_last_expr, "<stdin>", "eval", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        _exec_ret = eval(_exec_code, globals())
        if inspect.iscoroutine(_exec_ret):
            await _exec_ret
        _eval_ret = eval(_eval_code, globals())
        _eval_res = (await _eval_ret) if inspect.iscoroutine(_eval_ret) else _eval_ret
    else:
        _exec_code = compile(_parsed, "<stdin>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        _exec_ret = eval(_exec_code, globals())
        if inspect.iscoroutine(_exec_ret):
            await _exec_ret
except (Exception, SystemExit) as _exc:
    if not isinstance(_exc, SystemExit) or _exc.code not in (None, 0):
        _tb_str = traceback.format_exc()
finally:
    sys.stdout = _old_out
    sys.stderr = _old_err

{
    "stdout": _stdout_buf.getvalue(),
    "stderr": _stderr_buf.getvalue() + _tb_str,
    "result": repr(_eval_res) if _eval_res is not None else None,
    "error": bool(_tb_str)
}
`;
          const resProxy = await mainThreadPyodide.runPythonAsync(runnerScript);
          const resObj = resProxy.toJs();
          resProxy.destroy();
          callback({
            stdout: resObj.get ? resObj.get("stdout") : resObj.stdout,
            stderr: resObj.get ? resObj.get("stderr") : resObj.stderr,
            result: resObj.get ? resObj.get("result") : resObj.result,
            error: resObj.get ? Boolean(resObj.get("error")) : Boolean(resObj.error),
          });
        } catch (err) {
          callback({
            stdout: "",
            stderr: err.toString(),
            result: null,
            error: true,
          });
        }
      });
    }
  }

  function updateLaunchButtons(status, text) {
    document
      .querySelectorAll(".thebe-launch-button, .pyodide-launch-button")
      .forEach((btn) => {
        btn.classList.remove(
          "thebe-status-loading",
          "thebe-status-building",
          "thebe-status-ready",
          "thebe-status-failed"
        );
        btn.classList.add(`thebe-status-${status}`);
        btn.setAttribute("aria-busy", status === "loading" ? "true" : "false");
        btn.title = text;

        const loadingText = btn.querySelector(".loading-text");
        if (loadingText) {
          loadingText.innerHTML = `<span class="launch_msg">Pyodide (WebAssembly): </span><span class="status">${status}</span>`;
        } else {
          btn.textContent = text;
        }
      });
  }

  function transformCodeCells() {
    const config = getConfig();
    const selector = config.selector || "div.highlight-python";

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
      runBtn.title = pyodideReady
        ? "Execute cell (Shift+Enter)"
        : "Runtime initializing, please wait...";

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
          if (
            res.error !== undefined && res.error !== null
              ? Boolean(res.error)
              : Boolean(res.stderr && res.stderr.trim())
          ) {
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

  if (typeof window !== "undefined") {
    window.pyodideRuntime = {
      on: on,
      trigger: trigger,
      bootstrap: bootstrap,
      getConfig: getConfig,
      status: () => (pyodideReady ? "ready" : pyodideInitializing ? "loading" : "idle"),
    };
    window.thebelab = window.pyodideRuntime;

    window.initPyodide = function () {
      bootstrap();
    };
    window.initThebe = window.initPyodide;
  }

  function bindLaunchButtons() {
    if (typeof document === "undefined") return;
    document
      .querySelectorAll(".thebe-launch-button, .pyodide-launch-button")
      .forEach((btn) => {
        if (!btn.getAttribute("onclick") && !btn.dataset.pyodideBound) {
          btn.dataset.pyodideBound = "true";
          btn.addEventListener("click", bootstrap);
        }
      });
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", bindLaunchButtons);
    } else {
      bindLaunchButtons();
    }
  }
})();
