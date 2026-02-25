(() => {
  "use strict";

  class ApiChatAdapter {
    constructor(options = {}) {
      this.baseUrl = options.baseUrl || "/api";
      this._sessionId = null;
    }

    async createSession() {
      const response = await fetch(`${this.baseUrl}/sessions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!response.ok) {
        throw new Error(`createSession failed: ${response.status}`);
      }

      const data = await response.json();
      this._sessionId = data.session_id;
      return data;
    }

    async submitTurn(payload = {}) {
      if (!this._sessionId && payload.session_id) {
        this._sessionId = payload.session_id;
      }
      if (!this._sessionId) {
        throw new Error("session_id is required");
      }

      const body = {
        message: String(payload.message || ""),
        ui_brand_selection: payload.ui_brand_selection || null,
        structured_updates: payload.structured_updates || {},
        feedback: payload.feedback || {},
      };

      const response = await fetch(`${this.baseUrl}/sessions/${this._sessionId}/turn`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      if (!response.ok) {
        throw new Error(`submitTurn failed: ${response.status}`);
      }

      return response.json();
    }

    async streamRun(runId, onEvent) {
      if (!this._sessionId || !runId) {
        return;
      }

      await new Promise((resolve, reject) => {
        const url = `${this.baseUrl}/sessions/${this._sessionId}/runs/${runId}/events`;
        const source = new EventSource(url);

        const relay = (eventName) => (event) => {
          let data = {};
          try {
            data = event && event.data ? JSON.parse(event.data) : {};
          } catch (_error) {
            data = {};
          }
          if (typeof onEvent === "function") {
            onEvent({ event: eventName, ...data });
          }
          if (eventName === "done" || eventName === "error") {
            source.close();
            resolve();
          }
        };

        source.addEventListener("phase_started", relay("phase_started"));
        source.addEventListener("browser_live", relay("browser_live"));
        source.addEventListener("error", relay("error"));
        source.addEventListener("done", relay("done"));

        source.onerror = () => {
          source.close();
          reject(new Error("SSE connection failed"));
        };
      });
    }

    async getResult(runId) {
      if (!this._sessionId || !runId) {
        throw new Error("run_id is required");
      }

      const deadline = Date.now() + 30_000;
      while (Date.now() < deadline) {
        const response = await fetch(
          `${this.baseUrl}/sessions/${this._sessionId}/runs/${runId}/result`,
          { method: "GET" },
        );

        if (response.status === 202) {
          await sleep(250);
          continue;
        }

        if (!response.ok) {
          throw new Error(`getResult failed: ${response.status}`);
        }

        return response.json();
      }

      throw new Error("getResult timeout");
    }
  }

  function sleep(ms) {
    return new Promise((resolve) => {
      window.setTimeout(resolve, ms);
    });
  }

  window.ApiChatAdapter = ApiChatAdapter;
})();
