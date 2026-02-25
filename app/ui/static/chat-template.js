(() => {
  "use strict";

  const PHASE_MESSAGE_FADE_MS = 220;
  const SUPPORTED_BRANDS = ["UNIQLO", "GU"];
  const BROWSER_LIVE_RENDER_INTERVAL_MS = 120;

  class ChatTemplateUI {
    constructor(root) {
      this.root = root;
      this.timeline = root.querySelector("#timeline");

      this.profileForm = root.querySelector("#profile-form");
      this.profileError = root.querySelector("#profile-error");
      this.profileSubmit = root.querySelector("#profile-submit");
      this.scenarioInput = root.querySelector("#profile-scenario");
      this.primarySceneInput = root.querySelector("#profile-primary-scene");
      this.preferencesInput = root.querySelector("#profile-preferences");
      this.exclusionsInput = root.querySelector("#profile-exclusions");
      this.brandSelect = root.querySelector("#brand-select");
      this.brandOtherWrap = root.querySelector("#profile-brand-other-wrap");
      this.brandOtherInput = root.querySelector("#profile-brand-other");

      this.chatComposer = root.querySelector("#chat-composer");
      this.form = root.querySelector("#composer");
      this.input = root.querySelector("#composer-input");
      this.sendButton = root.querySelector(".pill-send");

      this.state = "idle";
      this.busy = false;
      this.sessionId = null;
      this.activePhaseNode = null;
      this.activePhaseName = "";
      this.activePhaseBadge = null;
      this.activePhaseLabel = null;
      this.activePhaseElapsedNode = null;
      this.phaseTransition = Promise.resolve();

      this.profileCompleted = false;
      this.pendingModifyOutfit = null;
      this.selectedOutfitId = null;
      this.browserLiveWindow = null;
      this.browserLiveFrames = [];
      this.browserCloudLiveUrl = "";
      this.browserLiveRenderScheduled = false;
      this.browserLiveLastRenderAt = 0;
      this.browserPhaseElapsedStartedAt = 0;
      this.browserPhaseElapsedTimerId = null;
      this.browserFoundItemIds = new Set();
      this.browserFoundCount = 0;

      if (typeof window.ApiChatAdapter === "function") {
        this.adapter = new window.ApiChatAdapter();
      } else {
        this.adapter = null;
      }
    }

    init() {
      if (!this.timeline || !this.profileForm || !this.form || !this.input || !this.sendButton) {
        return;
      }

      this.bindEvents();
      this.autoResizeInput();
      this.updateBrandOtherField();
      this.hideProfileError();
      this.updateSubmitStates();

      if (!this.adapter) {
        this.showProfileError("系統資源尚未載入，請重新整理後再試。", true);
        return;
      }
    }

    bindEvents() {
      this.profileForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        await this.handleProfileSubmit();
      });

      this.form.addEventListener("submit", async (event) => {
        event.preventDefault();
        await this.handleChatSubmit();
      });

      if (this.brandSelect) {
        this.brandSelect.addEventListener("change", () => {
          this.updateBrandOtherField();
          this.updateSubmitStates();
        });
      }

      for (const field of [
        this.scenarioInput,
        this.primarySceneInput,
        this.preferencesInput,
        this.exclusionsInput,
        this.brandOtherInput,
      ]) {
        if (!field) {
          continue;
        }
        field.addEventListener("input", () => {
          this.hideProfileError();
          this.updateSubmitStates();
        });
      }

      this.input.addEventListener("input", () => {
        this.autoResizeInput();
      });

      this.input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          this.form.requestSubmit();
        }
      });
    }

    async handleProfileSubmit() {
      if (this.busy || this.profileCompleted) {
        return;
      }

      this.hideProfileError();
      const collected = this.collectProfileUpdates();
      if (!collected.ok) {
        this.showProfileError(collected.error || "請補齊必填資料。");
        return;
      }

      const summary = this.composeInitialSummary(collected.structuredUpdates);
      const confirmation = this.composeProfileConfirmation(collected.structuredUpdates);
      if (confirmation) {
        this.pushTextMessage("user", confirmation);
      }
      await this.runTurn({
        message: summary,
        ui_brand_selection: collected.uiBrandSelection,
        structured_updates: collected.structuredUpdates,
        feedback: {},
      }, { fromProfile: true });
    }

    async handleChatSubmit() {
      if (this.busy || !this.profileCompleted) {
        return;
      }

      const message = this.input.value.trim();
      if (!message) {
        this.updateSubmitStates();
        return;
      }

      this.pushTextMessage("user", message);
      this.input.value = "";
      this.autoResizeInput();

      if (this.pendingModifyOutfit) {
        const target = this.pendingModifyOutfit;
        this.pendingModifyOutfit = null;

        await this.runTurn({
          message,
          ui_brand_selection: null,
          structured_updates: {},
          feedback: {
            action: "modify",
            selected_outfit_id: target.outfit_id,
            preserve_outfit_id: target.outfit_id,
            reason: message,
            replace_categories: this.detectReplaceCategories(message),
          },
        }, { fromProfile: false });
        return;
      }

      await this.runTurn({
        message,
        ui_brand_selection: null,
        structured_updates: {},
        feedback: {},
      }, { fromProfile: false });
    }

    async runTurn(payload, options = { fromProfile: false }) {
      this.setState("running");
      try {
        if (!this.sessionId) {
          const session = await this.adapter.createSession();
          this.sessionId = session.session_id;
        }

        const turn = await this.adapter.submitTurn({
          session_id: this.sessionId,
          message: String(payload.message || ""),
          ui_brand_selection: payload.ui_brand_selection || null,
          structured_updates: payload.structured_updates || {},
          feedback: payload.feedback || {},
        });

        await this.handleTurnResponse(turn, options);
      } catch (error) {
        const messageText = error && error.message ? error.message : "未知錯誤";
        if (options.fromProfile && !this.profileCompleted) {
          this.showProfileError("流程執行失敗，請稍後再試。");
        } else {
          this.pushTextMessage("assistant", `流程失敗：${messageText}`);
        }
      }
      this.setState("completed");
    }

    async handleTurnResponse(turn, options = { fromProfile: false }) {
      const status = turn && turn.status ? turn.status : "error";

      if (status !== "run_started") {
        if (options.fromProfile && !this.profileCompleted) {
          const reason =
            (turn && typeof turn.pending_question === "string" && turn.pending_question.trim()) ||
            (turn && typeof turn.assistant_message === "string" && turn.assistant_message.trim()) ||
            "資料尚未通過檢查，請修正後再試一次。";
          this.showProfileError(reason);
        } else {
          const assistantMessage = turn && turn.assistant_message ? turn.assistant_message : "";
          if (assistantMessage) {
            this.pushTextMessage("assistant", assistantMessage);
          }
        }
        this.updateSubmitStates();
        return;
      }

      if (options.fromProfile && !this.profileCompleted) {
        this.activateChatMode();
      }

      const assistantMessage = turn && turn.assistant_message ? turn.assistant_message : "";
      if (!options.fromProfile && assistantMessage) {
        this.pushTextMessage("assistant", assistantMessage);
      }

      const runId = turn.run_id;
      if (!runId) {
        this.pushTextMessage("assistant", "run_id 不存在，無法繼續執行。");
        return;
      }

      this.browserLiveFrames = [];
      this.browserCloudLiveUrl = "";
      this.browserFoundItemIds = new Set();
      this.browserFoundCount = 0;
      this.stopBrowserPhaseTimer();
      this.updateBrowsePhaseLiveUi();
      if (this.browserLiveWindow && !this.browserLiveWindow.closed) {
        this.browserLiveWindow.close();
      }
      this.browserLiveWindow = null;

      await this.adapter.streamRun(runId, (eventData) => {
        this.handleStreamEvent(eventData);
      });
      await this.phaseTransition;

      const result = await this.adapter.getResult(runId);
      await this.renderResult(result);
    }

    activateChatMode() {
      this.profileCompleted = true;
      this.hideProfileError();

      if (this.profileForm && this.profileForm.parentNode) {
        this.profileForm.parentNode.removeChild(this.profileForm);
      }

      if (this.chatComposer) {
        this.chatComposer.hidden = false;
      }

      this.input.focus();
      this.updateSubmitStates();
    }

    handleStreamEvent(eventData) {
      if (!eventData || typeof eventData !== "object") {
        return;
      }

      if (eventData.event === "phase_started") {
        const phase = eventData.phase || "collect";
        const text = this.resolvePhaseMessage(phase, eventData.output_summary);
        this.enqueuePhaseMessage(text, phase);
        return;
      }

      if (eventData.event === "browser_live") {
        this.handleBrowserLiveEvent(eventData);
        return;
      }

      if (eventData.event === "error") {
        this.pushTextMessage("assistant", `流程錯誤：${eventData.message || "請稍後重試"}`);
      }
    }

    resolvePhaseMessage(phase, fallbackText) {
      const normalized = String(phase || "").trim().toLowerCase();
      if (normalized === "design" || normalized === "designer") {
        return "設計團隊瘋狂工作中...";
      }
      if (normalized === "review" || normalized === "reviewer") {
        return "設計團隊激烈溝通中...";
      }
      if (normalized === "plan" || normalized === "planner") {
        return "我來看看哪裡有這些衣服...";
      }
      if (normalized === "browse" || normalized === "browser") {
        return "可以看我怎麼找的";
      }

      return fallbackText || `流程進行中（${phase}）...`;
    }

    async renderResult(result) {
      await this.clearActivePhaseMessage();

      const summary =
        result && typeof result.assistant_message === "string" && result.assistant_message.trim()
          ? result.assistant_message.trim()
          : "流程完成，以下是穿搭結果。";

      this.pushTextMessage("assistant", summary);

      const cards = result && Array.isArray(result.outfit_cards) ? result.outfit_cards : [];
      const status = String(result && result.status ? result.status : "").trim().toLowerCase();
      if (status === "completed") {
        const products = this.collectProducts(cards);
        if (products.length > 0) {
          this.pushProductResults(products);
        } else {
          this.pushTextMessage("assistant", "目前尚未取得可展示的商品結果。");
        }
      } else if (cards.length > 0) {
        this.pushCards(cards);
      }

      const issues = result && Array.isArray(result.issues) ? result.issues : [];
      if (issues.length > 0) {
        this.pushTextMessage("assistant", `注意事項：\n- ${issues.join("\n- ")}`);
      }
    }

    pushTextMessage(role, text, isSticky = false) {
      const article = document.createElement("article");
      article.className = `msg msg-${role}`;

      const bubble = document.createElement("div");
      bubble.className = `bubble bubble-${role}`;

      const paragraph = document.createElement("p");
      paragraph.className = "msg-text";
      paragraph.textContent = text;

      bubble.appendChild(paragraph);
      article.appendChild(bubble);
      this.mountMessage(article, isSticky);
    }

    enqueuePhaseMessage(text, phase = "") {
      this.phaseTransition = this.phaseTransition.then(() => this.pushSystemPhase(text, phase));
      return this.phaseTransition;
    }

    async pushSystemPhase(text, phase = "") {
      await this.clearActivePhaseMessage();

      const article = document.createElement("article");
      article.className = "msg msg-system msg-system-phase";

      const bubble = document.createElement("div");
      bubble.className = "bubble bubble-system";

      const badge = document.createElement("p");
      badge.className = "phase-chip";
      const label = document.createElement("span");
      label.className = "phase-label";
      label.textContent = text;
      const elapsed = document.createElement("span");
      elapsed.className = "phase-elapsed";
      badge.appendChild(label);
      badge.appendChild(elapsed);

      const normalizedPhase = String(phase || "").trim().toLowerCase();

      bubble.appendChild(badge);
      article.appendChild(bubble);
      this.mountMessage(article);
      this.activePhaseNode = article;
      this.activePhaseName = normalizedPhase;
      this.activePhaseBadge = badge;
      this.activePhaseLabel = label;
      this.activePhaseElapsedNode = elapsed;
      if (normalizedPhase === "browse" || normalizedPhase === "browser") {
        this.startBrowserPhaseTimer(true);
      } else {
        this.stopBrowserPhaseTimer();
      }
      this.updateBrowsePhaseLiveUi();
    }

    pushCards(cards) {
      const article = document.createElement("article");
      article.className = "msg msg-assistant";

      const bubble = document.createElement("div");
      bubble.className = "bubble bubble-assistant";

      const shell = document.createElement("div");
      shell.className = "result-shell";

      const heading = document.createElement("p");
      heading.className = "result-title";
      heading.textContent = "推薦結果";

      const grid = document.createElement("div");
      grid.className = "result-grid";

      cards.forEach((card, index) => {
        grid.appendChild(this.buildCard(card, index));
      });

      shell.appendChild(heading);
      shell.appendChild(grid);
      bubble.appendChild(shell);
      article.appendChild(bubble);
      this.mountMessage(article);
      this.markSelectedOutfit(this.selectedOutfitId);
    }

    collectProducts(cards) {
      const flattened = [];
      for (const card of Array.isArray(cards) ? cards : []) {
        const products = Array.isArray(card && card.products) ? card.products : [];
        for (const product of products) {
          if (!product) {
            continue;
          }
          flattened.push(product);
        }
      }
      return flattened;
    }

    createProductImageNode(product, titleText) {
      const screenshot = String(product && product.screenshot_base64 ? product.screenshot_base64 : "").trim();
      if (!screenshot) {
        return null;
      }

      const dataUrl = `data:image/png;base64,${screenshot}`;
      const altText = `${titleText} 截圖`;
      const cropBox = this.normalizeCropBox(product && product.crop_box ? product.crop_box : null);
      if (!cropBox) {
        const image = document.createElement("img");
        image.className = "card-product-image";
        image.alt = altText;
        image.src = dataUrl;
        return image;
      }

      const container = document.createElement("div");
      container.className = "card-product-image-wrap";

      const canvas = document.createElement("canvas");
      canvas.className = "card-product-image";
      canvas.style.display = "none";

      const fallback = document.createElement("img");
      fallback.className = "card-product-image";
      fallback.alt = altText;
      fallback.src = dataUrl;
      fallback.style.display = "none";

      container.appendChild(canvas);
      container.appendChild(fallback);

      const source = new Image();
      source.decoding = "async";
      source.onload = () => {
        const naturalWidth = Number(source.naturalWidth || 0);
        const naturalHeight = Number(source.naturalHeight || 0);
        if (naturalWidth <= 1 || naturalHeight <= 1) {
          fallback.style.display = "block";
          return;
        }

        const sx = Math.max(0, Math.min(naturalWidth - 1, Math.round(cropBox.x * naturalWidth)));
        const sy = Math.max(0, Math.min(naturalHeight - 1, Math.round(cropBox.y * naturalHeight)));
        const sw = Math.max(1, Math.min(naturalWidth - sx, Math.round(cropBox.width * naturalWidth)));
        const sh = Math.max(1, Math.min(naturalHeight - sy, Math.round(cropBox.height * naturalHeight)));

        canvas.width = sw;
        canvas.height = sh;
        const ctx = canvas.getContext("2d");
        if (!ctx) {
          fallback.style.display = "block";
          return;
        }
        ctx.drawImage(source, sx, sy, sw, sh, 0, 0, sw, sh);
        canvas.style.display = "block";
      };
      source.onerror = () => {
        fallback.style.display = "block";
      };
      source.src = dataUrl;

      return container;
    }

    normalizeCropBox(raw) {
      if (!raw) {
        return null;
      }
      let x = 0;
      let y = 0;
      let width = 0;
      let height = 0;
      if (Array.isArray(raw) && raw.length === 4) {
        x = Number(raw[0]);
        y = Number(raw[1]);
        width = Number(raw[2]);
        height = Number(raw[3]);
      } else if (typeof raw === "object") {
        x = Number(raw.x);
        y = Number(raw.y);
        width = Number(raw.width);
        height = Number(raw.height);
      } else {
        return null;
      }
      if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(width) || !Number.isFinite(height)) {
        return null;
      }

      let nx = x;
      let ny = y;
      let nw = width;
      let nh = height;
      if ((nx > 1 || ny > 1 || nw > 1 || nh > 1) && nx <= 100 && ny <= 100 && nw <= 100 && nh <= 100) {
        nx = nx / 100;
        ny = ny / 100;
        nw = nw / 100;
        nh = nh / 100;
      }

      nx = Math.min(Math.max(nx, 0), 1);
      ny = Math.min(Math.max(ny, 0), 1);
      nw = Math.min(Math.max(nw, 0), 1);
      nh = Math.min(Math.max(nh, 0), 1);
      if (nw <= 0 || nh <= 0 || nx >= 1 || ny >= 1) {
        return null;
      }
      if (nx + nw > 1) {
        nw = 1 - nx;
      }
      if (ny + nh > 1) {
        nh = 1 - ny;
      }
      if (nw <= 0 || nh <= 0) {
        return null;
      }

      return { x: nx, y: ny, width: nw, height: nh };
    }

    pushProductResults(products) {
      const article = document.createElement("article");
      article.className = "msg msg-assistant";

      const bubble = document.createElement("div");
      bubble.className = "bubble bubble-assistant";

      const shell = document.createElement("div");
      shell.className = "result-shell";

      const heading = document.createElement("p");
      heading.className = "result-title";
      heading.textContent = "找到的接近搭配商品";

      const productList = document.createElement("ol");
      productList.className = "result-product-list";

      products.forEach((product) => {
        const item = document.createElement("li");
        item.className = "result-product-item";

        const block = document.createElement("article");
        block.className = "card-product";

        const title = document.createElement("p");
        title.className = "card-product-title";
        title.textContent = String(product && product.title ? product.title : "未命名商品");

        const imageNode = this.createProductImageNode(product, title.textContent);
        if (imageNode) {
          block.appendChild(imageNode);
        }

        const link = document.createElement("a");
        link.className = "card-product-link";
        link.href = String(product && product.url ? product.url : "#");
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "查看商品連結";

        block.appendChild(title);
        block.appendChild(link);
        item.appendChild(block);
        productList.appendChild(item);
      });

      shell.appendChild(heading);
      shell.appendChild(productList);
      bubble.appendChild(shell);
      article.appendChild(bubble);
      this.mountMessage(article);
    }

    buildCard(card, index) {
      const element = document.createElement("article");
      element.className = "result-card";
      element.style.setProperty("--card-index", String(index));
      if (card && card.outfit_id) {
        element.dataset.outfitId = String(card.outfit_id);
      }
      if (this.selectedOutfitId && card && card.outfit_id === this.selectedOutfitId) {
        element.classList.add("is-selected");
      }

      const title = document.createElement("h3");
      title.textContent = this.sanitizeCardStyle(card && card.style ? card.style : "");

      const rationale = document.createElement("p");
      rationale.className = "card-rationale";
      rationale.textContent = this.sanitizeCardRationale(card && card.rationale ? card.rationale : "");

      element.appendChild(title);
      element.appendChild(rationale);

      const outfitItems = Array.isArray(card && card.items) ? card.items : [];
      if (outfitItems.length > 0) {
        const outfitHeading = document.createElement("p");
        outfitHeading.className = "card-range";
        outfitHeading.textContent = "部件明細：";

        const outfitList = document.createElement("ul");
        outfitList.className = "card-items";
        for (const item of outfitItems) {
          const li = document.createElement("li");
          const category = this.categoryLabel(item && item.category ? item.category : "");
          const color = item && item.color ? item.color : "--";
          const effect = item && item.visual_effect ? item.visual_effect : "--";
          li.textContent = `${category}：${color} / ${effect}`;
          outfitList.appendChild(li);
        }

        element.appendChild(outfitHeading);
        element.appendChild(outfitList);
      }

      const productItems = Array.isArray(card && card.products) ? card.products : [];
      if (productItems.length > 0) {
        const productHeading = document.createElement("p");
        productHeading.className = "card-range";
        productHeading.textContent = "搜尋結果：";

        const productList = document.createElement("div");
        productList.className = "card-products";

        for (const product of productItems) {
          const block = document.createElement("article");
          block.className = "card-product";

          const title = document.createElement("p");
          title.className = "card-product-title";
          title.textContent = String(product && product.title ? product.title : "未命名商品");

          const imageNode = this.createProductImageNode(product, title.textContent);
          if (imageNode) {
            block.appendChild(imageNode);
          }

          const link = document.createElement("a");
          link.className = "card-product-link";
          link.href = String(product && product.url ? product.url : "#");
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = "查看商品連結";

          block.appendChild(title);
          block.appendChild(link);
          productList.appendChild(block);
        }

        element.appendChild(productHeading);
        element.appendChild(productList);
      }

      if (card && card.outfit_id) {
        const actions = document.createElement("div");
        actions.className = "card-actions";

        const searchBtn = document.createElement("button");
        searchBtn.type = "button";
        searchBtn.className = "quick-prompt";
        searchBtn.textContent = "搜尋這套";
        searchBtn.addEventListener("click", async () => {
          await this.handleCardAction(card, "search", element);
        });

        const modifyBtn = document.createElement("button");
        modifyBtn.type = "button";
        modifyBtn.className = "quick-prompt";
        modifyBtn.textContent = "修改這套";
        modifyBtn.addEventListener("click", async () => {
          await this.handleCardAction(card, "modify", element);
        });

        actions.appendChild(searchBtn);
        actions.appendChild(modifyBtn);
        element.appendChild(actions);
      }

      return element;
    }

    async handleCardAction(card, action, cardElement = null) {
      if (this.busy || !card || !card.outfit_id) {
        return;
      }

      this.pendingModifyOutfit = null;
      this.markSelectedOutfit(card.outfit_id, cardElement);

      if (action === "search") {
        const message = `我要搜尋「${card.title || "這套"}」`;
        this.pushTextMessage("user", message);
        await this.runTurn({
          message,
          ui_brand_selection: null,
          structured_updates: {},
          feedback: {
            action: "search",
            selected_outfit_id: card.outfit_id,
          },
        }, { fromProfile: false });
        return;
      }

      this.pendingModifyOutfit = { outfit_id: card.outfit_id, title: card.title || "這套" };
      this.pushTextMessage(
        "assistant",
        `你選擇修改「${this.pendingModifyOutfit.title}」。請在下方輸入想怎麼修改（例如：鞋子太正式，改成更休閒）。`,
      );
      this.input.focus();
      this.updateSubmitStates();
    }

    detectReplaceCategories(message) {
      const text = String(message || "").toLowerCase();
      const detected = [];

      const rules = [
        { values: ["外套", "jacket", "coat", "outerwear"], category: "外套" },
        { values: ["上身", "上衣", "shirt", "top", "tee", "t-shirt", "tshirt"], category: "上身" },
        { values: ["下身", "褲", "pants", "trousers", "bottom"], category: "下身" },
        { values: ["鞋", "鞋子", "shoe", "shoes", "sneaker", "sneakers"], category: "鞋子" },
        { values: ["配件", "accessory", "accessories"], category: "配件" },
      ];

      for (const rule of rules) {
        if (rule.values.some((token) => text.includes(token))) {
          detected.push(rule.category);
        }
      }

      return Array.from(new Set(detected));
    }

    collectProfileUpdates() {
      const structuredUpdates = {};

      const scenario = this.valueOf(this.scenarioInput);
      const primaryScene = this.valueOf(this.primarySceneInput);
      const preferences = this.parseCommaList(this.valueOf(this.preferencesInput));
      const exclusions = this.parseCommaList(this.valueOf(this.exclusionsInput));
      const uiBrandSelection = this.selectedBrand();
      const otherBrand = this.valueOf(this.brandOtherInput);

      if (scenario) {
        structuredUpdates.scenario = scenario;
      }
      if (primaryScene) {
        structuredUpdates.primary_scene = primaryScene;
      }
      if (preferences.length > 0) {
        structuredUpdates.preferences = preferences;
      }
      if (exclusions.length > 0) {
        structuredUpdates.exclusions = exclusions;
      }

      if (uiBrandSelection && uiBrandSelection !== "OTHER") {
        structuredUpdates.brand = uiBrandSelection;
      }
      if (uiBrandSelection === "OTHER" && otherBrand) {
        structuredUpdates.brand = otherBrand;
      }

      const missing = [];
      if (!this.hasText(structuredUpdates.scenario)) {
        missing.push("場合");
      }
      if (!this.hasText(structuredUpdates.primary_scene)) {
        missing.push("主要場景");
      }
      if (!this.hasText(structuredUpdates.brand)) {
        missing.push("品牌");
      }

      if (missing.length > 0) {
        return {
          ok: false,
          error: `請補齊必填欄位：${missing.join("、")}`,
          structuredUpdates,
          uiBrandSelection: uiBrandSelection || null,
        };
      }

      if (!this.isSupportedBrand(structuredUpdates.brand)) {
        return {
          ok: false,
          error: this.unsupportedBrandMessage(),
          structuredUpdates,
          uiBrandSelection: uiBrandSelection || null,
        };
      }

      return {
        ok: true,
        error: "",
        structuredUpdates,
        uiBrandSelection: uiBrandSelection || null,
      };
    }

    composeInitialSummary(structuredUpdates) {
      const parts = [
        `場合：${structuredUpdates.scenario || "-"}`,
        `主要場景：${structuredUpdates.primary_scene || "-"}`,
        `品牌：${structuredUpdates.brand || "-"}`,
      ];

      if (Array.isArray(structuredUpdates.preferences) && structuredUpdates.preferences.length > 0) {
        parts.push(`偏好：${structuredUpdates.preferences.join("、")}`);
      }
      if (Array.isArray(structuredUpdates.exclusions) && structuredUpdates.exclusions.length > 0) {
        parts.push(`避免：${structuredUpdates.exclusions.join("、")}`);
      }

      return parts.join("；");
    }

    composeProfileConfirmation(structuredUpdates) {
      const lines = [];
      if (this.hasText(structuredUpdates && structuredUpdates.scenario)) {
        lines.push(`場合：${String(structuredUpdates.scenario).trim()}`);
      }
      if (this.hasText(structuredUpdates && structuredUpdates.primary_scene)) {
        lines.push(`主要場景：${String(structuredUpdates.primary_scene).trim()}`);
      }
      if (this.hasText(structuredUpdates && structuredUpdates.brand)) {
        lines.push(`品牌：${String(structuredUpdates.brand).trim()}`);
      }
      if (Array.isArray(structuredUpdates && structuredUpdates.preferences) && structuredUpdates.preferences.length > 0) {
        lines.push(`偏好：${structuredUpdates.preferences.join("、")}`);
      }
      if (Array.isArray(structuredUpdates && structuredUpdates.exclusions) && structuredUpdates.exclusions.length > 0) {
        lines.push(`避免：${structuredUpdates.exclusions.join("、")}`);
      }
      if (lines.length <= 0) {
        return "";
      }
      return `${lines.join("\n")}`;
    }

    categoryLabel(rawCategory) {
      const normalized = String(rawCategory || "").trim().toLowerCase();
      if (["外套", "jacket", "coat", "outerwear"].includes(normalized)) {
        return "外套";
      }
      if (["上身", "上衣", "shirt", "top", "tee", "t-shirt", "tshirt"].includes(normalized)) {
        return "上身";
      }
      if (["下身", "褲", "bottom", "pants", "trousers"].includes(normalized)) {
        return "下身";
      }
      if (["鞋子", "鞋", "shoe", "shoes", "sneakers"].includes(normalized)) {
        return "鞋子";
      }
      if (["配件", "accessory", "accessories"].includes(normalized)) {
        return "配件";
      }
      return String(rawCategory || "部件");
    }

    mountMessage(node, isSticky = false) {
      this.root.classList.add("has-messages");
      this.timeline.appendChild(node);

      if (!isSticky) {
        this.scrollToBottom();
      }
    }

    setState(nextState) {
      this.state = nextState;
      this.root.dataset.state = nextState;
      this.busy = nextState === "running";
      this.updateSubmitStates();
    }

    autoResizeInput() {
      this.input.style.height = "auto";
      const maxHeight = 136;
      const nextHeight = Math.min(this.input.scrollHeight, maxHeight);
      this.input.style.height = `${Math.max(nextHeight, 32)}px`;
      this.updateSubmitStates();
    }

    updateSubmitStates() {
      const profileReady = this.collectProfileUpdates().ok;

      if (this.profileSubmit) {
        this.profileSubmit.disabled = this.busy || this.profileCompleted || !profileReady;
      }

      const hasChatInput = Boolean(this.input && this.input.value && this.input.value.trim());
      if (this.sendButton) {
        this.sendButton.disabled = this.busy || !this.profileCompleted || !hasChatInput;
      }

      if (this.input) {
        this.input.disabled = this.busy || !this.profileCompleted;
      }

      const profileFields = [
        this.scenarioInput,
        this.primarySceneInput,
        this.preferencesInput,
        this.exclusionsInput,
        this.brandSelect,
        this.brandOtherInput,
      ];
      for (const field of profileFields) {
        if (!field) {
          continue;
        }
        field.disabled = this.busy || this.profileCompleted;
      }
    }

    selectedBrand() {
      if (!this.brandSelect) {
        return "";
      }
      return String(this.brandSelect.value || "").trim();
    }

    isSupportedBrand(brand) {
      const token = String(brand || "").trim().toUpperCase();
      return SUPPORTED_BRANDS.includes(token);
    }

    unsupportedBrandMessage() {
      return "目前僅支援 UNIQLO 與 GU，其他品牌暫不支援使用。";
    }

    updateBrandOtherField() {
      const shouldShow = this.selectedBrand() === "OTHER";
      if (!this.brandOtherWrap || !this.brandOtherInput) {
        return;
      }
      this.brandOtherWrap.hidden = !shouldShow;
      if (!shouldShow) {
        this.brandOtherInput.value = "";
      }

      const unsupportedMessage = this.unsupportedBrandMessage();
      if (shouldShow && !this.profileCompleted && !this.busy) {
        this.showProfileError(unsupportedMessage, true);
      } else if (
        this.profileError &&
        String(this.profileError.textContent || "").trim() === unsupportedMessage
      ) {
        this.hideProfileError();
      }
    }

    showProfileError(message, sticky = false) {
      if (!this.profileError) {
        return;
      }
      this.profileError.textContent = this.localizeProfileError(message || "");
      this.profileError.hidden = !message;
      if (!sticky) {
        this.scrollToBottom();
      }
    }

    hideProfileError() {
      if (!this.profileError) {
        return;
      }
      this.profileError.hidden = true;
      this.profileError.textContent = "";
    }

    scrollToBottom() {
      this.timeline.scrollTop = this.timeline.scrollHeight;
    }

    clearActivePhaseMessage() {
      if (!this.activePhaseNode || !this.activePhaseNode.isConnected) {
        this.activePhaseNode = null;
        this.activePhaseName = "";
        this.activePhaseBadge = null;
        this.activePhaseLabel = null;
        this.activePhaseElapsedNode = null;
        this.stopBrowserPhaseTimer();
        return Promise.resolve();
      }

      const node = this.activePhaseNode;
      this.activePhaseNode = null;
      this.activePhaseName = "";
      this.activePhaseBadge = null;
      this.activePhaseLabel = null;
      this.activePhaseElapsedNode = null;
      this.stopBrowserPhaseTimer();
      node.classList.add("is-expiring");

      return new Promise((resolve) => {
        window.setTimeout(() => {
          if (node.parentNode) {
            node.parentNode.removeChild(node);
          }
          resolve();
        }, PHASE_MESSAGE_FADE_MS);
      });
    }

    handleBrowserLiveEvent(eventData) {
      const frame = {
        ts: Date.now(),
        item_id: String(eventData.item_id || "").trim(),
        item_category: String(eventData.item_category || "").trim(),
        round: Number(eventData.round || 0),
        step: Number(eventData.step || 0),
        status: String(eventData.status || "").trim().toLowerCase() || "in_progress",
        message: String(eventData.message || "").trim(),
        latest_url: String(eventData.latest_url || "").trim(),
        recent_actions: Array.isArray(eventData.recent_actions) ? eventData.recent_actions : [],
        recent_errors: Array.isArray(eventData.recent_errors) ? eventData.recent_errors : [],
        screenshot_base64: String(eventData.screenshot_base64 || "").trim(),
        live_url: String(eventData.live_url || "").trim(),
        elapsed_seconds: Number.isFinite(Number(eventData.elapsed_seconds))
          ? Number(eventData.elapsed_seconds)
          : null,
      };
      if (frame.live_url) {
        this.browserCloudLiveUrl = frame.live_url;
        this.updateBrowsePhaseLiveUi();
      }

      this.browserLiveFrames.push(frame);
      if (this.browserLiveFrames.length > 80) {
        this.browserLiveFrames.shift();
      }

      const foundStatuses = new Set(["success", "item_found", "found", "done", "completed"]);
      const foundByStatus = foundStatuses.has(frame.status);
      const foundByMessage = String(frame.message || "").toLowerCase() === "ok" && Boolean(frame.latest_url);
      if ((foundByStatus || foundByMessage) && frame.item_id && !this.browserFoundItemIds.has(frame.item_id)) {
        this.browserFoundItemIds.add(frame.item_id);
        this.browserFoundCount += 1;
        const foundIndex = this.browserFoundCount;
        const elapsedLabel = this.resolveFoundItemElapsedText(frame);
        this.pushTextMessage("assistant", `已找到第 ${foundIndex} 個商品，耗時 ${elapsedLabel}`);
        this.startBrowserPhaseTimer(true);
      }
      this.scheduleBrowserLiveRender();
    }

    openBrowserLiveWindow() {
      if (!this.browserCloudLiveUrl) {
        return;
      }

      const direct = window.open(this.browserCloudLiveUrl, "_blank", "noopener,noreferrer");
      if (!direct) {
        return;
      }
    }

    updateBrowsePhaseLiveUi() {
      const phase = String(this.activePhaseName || "").toLowerCase();
      const isBrowsePhase = phase === "browse" || phase === "browser";
      const badge = this.activePhaseBadge;
      const label = this.activePhaseLabel;
      const elapsed = this.activePhaseElapsedNode;
      if (!isBrowsePhase || !badge || !label || !badge.isConnected || !label.isConnected) {
        if (elapsed) {
          elapsed.textContent = "";
        }
        return;
      }
      this.updateBrowserPhaseElapsedText();

      const existing = badge.querySelector(".phase-live-button");
      if (!this.browserCloudLiveUrl) {
        label.textContent = "可以看我怎麼找的";
        if (existing && existing.parentNode) {
          existing.parentNode.removeChild(existing);
        }
        return;
      }

      label.textContent = "可以看我怎麼找的（開啟直播）";
      if (existing) {
        existing.title = `開啟官方 Live URL: ${this.browserCloudLiveUrl}`;
        existing.dataset.liveUrl = this.browserCloudLiveUrl;
        return;
      }

      const liveBtn = document.createElement("button");
      liveBtn.type = "button";
      liveBtn.className = "phase-live-button";
      liveBtn.setAttribute("aria-label", "開啟官方 Live URL");
      liveBtn.title = `開啟官方 Live URL: ${this.browserCloudLiveUrl}`;
      liveBtn.dataset.liveUrl = this.browserCloudLiveUrl;
      liveBtn.innerHTML = "<span class='phase-live-icon' aria-hidden='true'>📺</span><span>開啟直播</span>";
      liveBtn.addEventListener("click", () => {
        this.openBrowserLiveWindow();
      });
      badge.appendChild(liveBtn);
    }

    startBrowserPhaseTimer(reset = false) {
      const phase = String(this.activePhaseName || "").toLowerCase();
      const isBrowsePhase = phase === "browse" || phase === "browser";
      if (!isBrowsePhase) {
        this.stopBrowserPhaseTimer();
        return;
      }
      if (reset || !this.browserPhaseElapsedStartedAt) {
        this.browserPhaseElapsedStartedAt = Date.now();
      }
      this.updateBrowserPhaseElapsedText();
      if (this.browserPhaseElapsedTimerId) {
        return;
      }
      this.browserPhaseElapsedTimerId = window.setInterval(() => {
        this.updateBrowserPhaseElapsedText();
      }, 1000);
    }

    stopBrowserPhaseTimer() {
      if (this.browserPhaseElapsedTimerId) {
        window.clearInterval(this.browserPhaseElapsedTimerId);
      }
      this.browserPhaseElapsedTimerId = null;
      this.browserPhaseElapsedStartedAt = 0;
      if (this.activePhaseElapsedNode) {
        this.activePhaseElapsedNode.textContent = "";
      }
    }

    updateBrowserPhaseElapsedText() {
      const elapsedNode = this.activePhaseElapsedNode;
      if (!elapsedNode || !elapsedNode.isConnected) {
        return;
      }
      const phase = String(this.activePhaseName || "").toLowerCase();
      const isBrowsePhase = phase === "browse" || phase === "browser";
      if (!isBrowsePhase || !this.browserPhaseElapsedStartedAt) {
        elapsedNode.textContent = "";
        return;
      }
      const seconds = Math.max(0, Math.floor((Date.now() - this.browserPhaseElapsedStartedAt) / 1000));
      elapsedNode.textContent = `已執行 ${this.formatDurationLabel(seconds)}...`;
    }

    resolveFoundItemElapsedText(frame) {
      const fromEvent = Number(frame && frame.elapsed_seconds);
      if (Number.isFinite(fromEvent) && fromEvent >= 0) {
        return this.formatDurationLabel(Math.round(fromEvent));
      }
      if (this.browserPhaseElapsedStartedAt) {
        const fallbackSeconds = Math.max(0, Math.floor((Date.now() - this.browserPhaseElapsedStartedAt) / 1000));
        return this.formatDurationLabel(fallbackSeconds);
      }
      return this.formatDurationLabel(0);
    }

    formatDurationLabel(totalSeconds) {
      const safeSeconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
      const minutes = Math.floor(safeSeconds / 60);
      const seconds = safeSeconds % 60;
      if (minutes <= 0) {
        return `${seconds} 秒`;
      }
      return `${minutes} 分 ${seconds} 秒`;
    }

    scheduleBrowserLiveRender(force = false) {
      if (force) {
        this.browserLiveRenderScheduled = false;
        this.browserLiveLastRenderAt = Date.now();
        this.renderBrowserLiveWindow();
        return;
      }
      if (this.browserLiveRenderScheduled) {
        return;
      }

      const elapsed = Date.now() - this.browserLiveLastRenderAt;
      const waitMs = Math.max(0, BROWSER_LIVE_RENDER_INTERVAL_MS - elapsed);
      this.browserLiveRenderScheduled = true;

      window.setTimeout(() => {
        this.browserLiveRenderScheduled = false;
        this.browserLiveLastRenderAt = Date.now();
        this.renderBrowserLiveWindow();
      }, waitMs);
    }

    renderBrowserLiveWindow() {
      if (!this.browserLiveWindow || this.browserLiveWindow.closed) {
        this.browserLiveWindow = null;
        return;
      }

      const doc = this.browserLiveWindow.document;
      if (this.browserCloudLiveUrl) {
        const cloudFrame = doc.getElementById("cloud-live-frame");
        const cloudStatus = doc.getElementById("cloud-live-status");
        if (!cloudFrame || !cloudStatus) {
          doc.open();
          doc.write(
            "<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8' />"
              + "<meta name='viewport' content='width=device-width, initial-scale=1' />"
              + "<title>Cloud 即時監看</title>"
              + "<style>"
              + "body{margin:0;background:#0f1319;color:#eef2f8;font-family:Segoe UI,Noto Sans TC,sans-serif;display:grid;grid-template-rows:auto 1fr;min-height:100vh;}"
              + ".top{display:flex;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid #2a3342;background:#141a23;}"
              + ".status{font-size:12px;color:#d7dfea;}"
              + ".link{margin-left:auto;color:#9dc4ff;text-decoration:none;font-size:12px;}"
              + ".link:hover{text-decoration:underline;}"
              + "iframe{border:0;width:100%;height:100%;background:#0c1016;}"
              + "</style></head><body>"
              + "<div class='top'>"
              + "<strong>Cloud 即時畫面</strong>"
              + "<span id='cloud-live-status' class='status'>連線中...</span>"
              + "<a id='cloud-live-open' class='link' target='_blank' rel='noopener noreferrer'>另開新分頁</a>"
              + "</div>"
              + "<iframe id='cloud-live-frame' allow='clipboard-read; clipboard-write; fullscreen'></iframe>"
              + "</body></html>"
          );
          doc.close();
        }

        const frameNode = doc.getElementById("cloud-live-frame");
        const statusNode = doc.getElementById("cloud-live-status");
        const openNode = doc.getElementById("cloud-live-open");
        if (!frameNode || !statusNode || !openNode) {
          return;
        }

        if (frameNode.getAttribute("src") !== this.browserCloudLiveUrl) {
          frameNode.setAttribute("src", this.browserCloudLiveUrl);
        }
        openNode.href = this.browserCloudLiveUrl;
        statusNode.textContent = "已連線，若畫面空白可點右側另開新分頁。";
        return;
      }

      const statusNode = doc.getElementById("live-status");
      const imageNode = doc.getElementById("live-image");
      const listNode = doc.getElementById("live-list");
      if (!statusNode || !imageNode || !listNode) {
        return;
      }

      const latest = this.browserLiveFrames[this.browserLiveFrames.length - 1] || null;
      if (!latest) {
        statusNode.textContent = "等待 browser 進度資料...";
        imageNode.style.display = "none";
        listNode.innerHTML = "";
        return;
      }

      statusNode.textContent = `最新狀態：${latest.item_category || latest.item_id || "目標"} / round ${latest.round || 1} / step ${latest.step || 1} / ${latest.status}`;
      if (latest.screenshot_base64) {
        imageNode.src = `data:image/png;base64,${latest.screenshot_base64}`;
        imageNode.style.display = "block";
      } else {
        imageNode.style.display = "none";
      }

      listNode.innerHTML = "";
      const recent = this.browserLiveFrames.slice(-24).reverse();
      for (const frame of recent) {
        const li = doc.createElement("li");
        const time = new Date(frame.ts).toLocaleTimeString("zh-TW", { hour12: false });
        const actions = frame.recent_actions.length > 0
          ? frame.recent_actions.join(" | ")
          : `step ${frame.step || 1} 執行中`;
        const errors = frame.recent_errors.length > 0 ? ` / error: ${frame.recent_errors.join(" | ")}` : "";
        li.textContent = `[${time}] ${frame.item_category || frame.item_id} r${frame.round || 1}: ${actions}${errors}`;
        if (frame.latest_url) {
          const link = doc.createElement("a");
          link.href = frame.latest_url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = " open";
          li.appendChild(link);
        }
        listNode.appendChild(li);
      }
    }

    parseCommaList(value) {
      return String(value || "")
        .split(/[,\uFF0C、]/)
        .map((item) => item.trim())
        .filter((item) => item);
    }

    valueOf(input) {
      if (!input) {
        return "";
      }
      return String(input.value || "").trim();
    }

    hasText(value) {
      return Boolean(String(value || "").trim());
    }

    sanitizeCardStyle(style) {
      const normalized = String(style || "")
        .replace(/回饋變化\s*[A-Z0-9]*/gi, "")
        .replace(/\s{2,}/g, " ")
        .trim();
      return normalized || "風格方案";
    }

    sanitizeCardRationale(rationale) {
      let normalized = String(rationale || "").trim();
      normalized = normalized.replace(/回饋變化\s*[A-Z0-9]*/gi, "");
      normalized = normalized.replace(/根據你的回饋/g, "");
      normalized = normalized.replace(/已依修改要求調整[:：]?/g, "");
      normalized = normalized.replace(/\s{2,}/g, " ").trim();
      return normalized || "此風格已依照你的場合與偏好完成搭配。";
    }

    markSelectedOutfit(outfitId, preferredNode = null) {
      this.selectedOutfitId = outfitId ? String(outfitId) : null;

      const cards = Array.from(this.root.querySelectorAll(".result-card[data-outfit-id]"));
      for (const card of cards) {
        card.classList.remove("is-selected");
      }

      if (!this.selectedOutfitId) {
        return;
      }

      const fallback = cards.find((node) => node.dataset.outfitId === this.selectedOutfitId) || null;
      const targetNode = preferredNode || fallback;
      if (targetNode) {
        targetNode.classList.add("is-selected");
      }
    }

    localizeProfileError(message) {
      const text = String(message || "").trim();
      if (!text) {
        return "";
      }

      const normalizedText = text
        .replace(/\s+(\d+\.\s+)/g, "\n$1")
        .replace(/請補充：\s+/g, "請補充：\n");

      const lines = normalizedText
        .split(/\r?\n/)
        .map((line) => String(line || "").trim())
        .filter((line) => line);

      return lines.map((line) => this.localizeProfileErrorLine(line)).join("\n");
    }

    localizeProfileErrorLine(line) {
      const numbered = String(line || "").match(/^(\d+)\.\s*(.+)$/);
      if (numbered) {
        return `${numbered[1]}. ${this.translateQuestionToZh(numbered[2])}`;
      }

      if (this.containsCjk(line)) {
        return line;
      }

      return this.translateGenericErrorToZh(line);
    }

    translateQuestionToZh(question) {
      const text = String(question || "").trim();
      const lowered = text.toLowerCase();

      if (lowered.includes("occasion") || lowered.includes("purpose") || lowered.includes("scenario")) {
        return "這次穿搭的場合或目的為何？（例如：商務會議、約會晚餐、日常通勤）";
      }
      if (
        lowered.includes("primary_scene") ||
        lowered.includes("scene") ||
        lowered.includes("where") ||
        lowered.includes("location")
      ) {
        return "這套穿搭主要會在哪個場景出現？（例如：室內辦公室、戶外街區、餐廳）";
      }
      if (lowered.includes("brand") || lowered.includes("shopping target") || lowered.includes("store")) {
        return "你偏好的品牌或購買目標是什麼？（例如：UNIQLO、GU）";
      }
      if (lowered.includes("preference")) {
        return "你希望保留哪些風格偏好？（例如：俐落、低彩度、寬鬆）";
      }
      if (lowered.includes("exclude") || lowered.includes("avoid") || lowered.includes("exclusion")) {
        return "你希望避免哪些元素？（例如：亮色、皮鞋、寬褲）";
      }
      return "請用一句話補充這次需求。";
    }

    translateGenericErrorToZh(text) {
      const lowered = String(text || "").toLowerCase();
      if (lowered.includes("adapter")) {
        return "系統資源尚未載入，請重新整理後再試。";
      }
      if (lowered.includes("missing") || lowered.includes("required")) {
        return "必填資料不足，請補齊後再試。";
      }
      if (lowered.includes("invalid")) {
        return "欄位內容格式不正確，請修正後再試。";
      }
      return "資料檢查未通過，請調整後再送出。";
    }

    containsCjk(text) {
      return /[\u3400-\u9fff]/.test(String(text || ""));
    }
  }

  window.addEventListener("DOMContentLoaded", () => {
    const root = document.querySelector(".chat-shell");
    if (!root) {
      return;
    }

    const app = new ChatTemplateUI(root);
    app.init();
  });
})();
