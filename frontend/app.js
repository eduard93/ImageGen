// Alpine component for the whole page. Kept in one file, plain JS, no build step.
// Edit freely — each method has a short comment saying what it does.

function app() {
  return {
    // ---- state ----
    settings: { models: [], resolutions: [], default_model: "", default_prompt: "" },
    settingsDraft: { gemini_api_key: "", default_prompt: "", modelsText: "",
                     default_model: "", useBatch: true },
    generations: [],
    current: null,
    library: [],
    composer: { prompt: "", model: "", resolution: "2K", num_images: 1, refs: [] },
    mention: { open: false, matches: [], start: -1 },
    showLibrary: false,
    libraryPicking: false,
    showSettings: false,
    submitting: false,
    dragging: false,    // true while an image is dragged over the composer
    lightbox: null,     // image URL shown full-size in an overlay, or null
    toast: "",
    _refSeq: 0,          // counter for default @Image<N> labels
    _pollTimers: {},     // gen id -> interval handle

    // ---- lifecycle ----
    async init() {
      await this.loadSettings();
      await this.loadGenerations();
      this.newGeneration();
      // Resume polling for anything still running.
      this.generations.forEach((g) => {
        if (g.status === "pending" || g.status === "running") this.watch(g.id);
      });
    },

    // ---- data loading ----
    async loadSettings() {
      this.settings = await api("/api/settings");
      this.composer.model = this.settings.default_model || (this.settings.models[0] || "");
      this.applyTheme(this.settings.theme || "dark");
    },
    async loadGenerations() {
      this.generations = await api("/api/generations");
    },
    async loadLibrary() {
      this.library = await api("/api/images?library=true");
    },

    // ---- generation navigation ----
    newGeneration() {
      this.current = null;
      this._refSeq = 0;
      const sysDefault = this.settings.system_instruction || "";
      this.composer = {
        prompt: this.settings.default_prompt || "",
        model: this.settings.default_model || (this.settings.models[0] || ""),
        resolution: this.settings.resolutions[1] || this.settings.resolutions[0] || "2K",
        num_images: 1,
        refs: [],
        // Prefill with the global default; on by default only if one exists.
        systemInstruction: sysDefault,
        useSystem: !!sysDefault.trim(),
      };
    },
    async selectGeneration(g) {
      this.current = await api(`/api/generations/${g.id}`);
      if (this.current.status === "pending" || this.current.status === "running") {
        this.watch(this.current.id);
      }
      this.$nextTick(() => this.scrollChat());
    },

    // Poll a running generation until it reaches a terminal state.
    watch(id) {
      if (this._pollTimers[id]) return;
      this._pollTimers[id] = setInterval(async () => {
        const g = await api(`/api/generations/${id}`);
        this.mergeGeneration(g);
        if (g.status !== "pending" && g.status !== "running") {
          clearInterval(this._pollTimers[id]);
          delete this._pollTimers[id];
        }
      }, 3000);
    },
    // Update both the sidebar entry and the open view when new data arrives.
    mergeGeneration(g) {
      const i = this.generations.findIndex((x) => x.id === g.id);
      if (i >= 0) this.generations[i] = g;
      else this.generations.unshift(g);
      if (this.current && this.current.id === g.id) this.current = g;
    },

    // Load the selected generation's prompt + references back into the composer.
    async editFromCurrent() {
      const src = this.current;           // capture before newGeneration() clears it
      if (!src) return;
      this.newGeneration();               // this sets this.current = null
      this.composer.prompt = src.prompt;
      this.composer.model = src.model;
      this.composer.resolution = src.resolution;
      this.composer.num_images = src.num_images;
      // Restore the source generation's system instruction (enabled if it had one).
      if (src.system_instruction) {
        this.composer.useSystem = true;
        this.composer.systemInstruction = src.system_instruction;
      } else {
        this.composer.useSystem = false;
      }
      for (const id of src.reference_image_ids) {
        try {
          const img = await api(`/api/images/${id}`);
          this.addRef(img);               // re-attach uploaded/library images
        } catch (_) { /* image may have been deleted; skip it */ }
      }
      this.$nextTick(() => this.$refs.prompt.focus());
    },

    // Give a generation a custom name (blank clears it → falls back to prompt).
    async renameGeneration(g) {
      const name = prompt("Name this generation:", g.name || "");
      if (name === null) return;  // user cancelled
      try {
        const updated = await api(`/api/generations/${g.id}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        this.mergeGeneration(updated);
      } catch (err) {
        this.notify(err.message || "Rename failed");
      }
    },

    async deleteGeneration(g) {
      if (!confirm("Delete this generation?")) return;
      await api(`/api/generations/${g.id}`, { method: "DELETE" });
      this.generations = this.generations.filter((x) => x.id !== g.id);
      if (this.current && this.current.id === g.id) this.newGeneration();
    },

    // ---- references (uploaded or picked from library) ----
    addRef(img) {
      // Library images use their custom name; uploads get @Image<N>.
      const label = img.library_name || `Image${++this._refSeq}`;
      this.composer.refs.push({
        key: `${img.id}-${label}`, id: img.id, label,
        url: img.url, inLibrary: !!img.in_library,
      });
      return label;
    },
    removeRef(ref) {
      this.composer.refs = this.composer.refs.filter((r) => r.key !== ref.key);
    },
    async attachFiles(event) {
      await this.handleFiles(Array.from(event.target.files || []));
      event.target.value = "";
    },
    // Drop images anywhere on the composer to attach them as references.
    async onDrop(event) {
      this.dragging = false;
      const files = Array.from(event.dataTransfer?.files || [])
        .filter((f) => f.type.startsWith("image/"));
      if (files.length) await this.handleFiles(files);
    },
    // Shared path for both the file picker and drag-and-drop.
    async handleFiles(files) {
      for (const file of files) {
        try {
          const form = new FormData();
          form.append("file", file);
          const img = await api("/api/images", { method: "POST", body: form });
          const label = this.addRef(img);
          this.appendMention(label); // drop a @ref into the prompt for convenience
        } catch (err) {
          this.notify(err.message || "Upload failed");
        }
      }
    },
    pickFromLibrary(img) {
      // Avoid adding the same library image twice.
      if (!this.composer.refs.some((r) => r.id === img.id)) {
        const label = this.addRef(img);
        this.appendMention(label);
      }
      this.showLibrary = false;
      this.libraryPicking = false;
    },

    // ---- @mention handling in the textarea ----
    onPromptInput(e) {
      const el = e.target;
      const upto = el.value.slice(0, el.selectionStart);
      const m = upto.match(/@([\w-]*)$/); // last @token before the cursor
      if (!m) { this.mention.open = false; return; }
      const q = m[1].toLowerCase();
      this.mention.start = el.selectionStart - m[0].length;
      // Suggest attached refs plus everything in the library.
      const seen = new Set();
      const suggestions = [];
      for (const r of this.composer.refs) {
        if (r.label.toLowerCase().startsWith(q) && !seen.has(r.label)) {
          seen.add(r.label); suggestions.push(r);
        }
      }
      for (const img of this.library) {
        const name = img.library_name || "";
        if (name.toLowerCase().startsWith(q) && !seen.has(name)) {
          seen.add(name);
          suggestions.push({ key: "lib" + img.id, id: img.id, label: name,
                             url: img.url, inLibrary: true });
        }
      }
      this.mention.matches = suggestions.slice(0, 8);
      this.mention.open = true;
    },
    applyMention(s) {
      // If a library suggestion isn't attached yet, attach it too.
      if (!this.composer.refs.some((r) => r.id === s.id)) this.addRef({
        id: s.id, url: s.url, library_name: s.inLibrary ? s.label : null,
        in_library: s.inLibrary,
      });
      const el = this.$refs.prompt;
      const before = el.value.slice(0, this.mention.start);
      const after = el.value.slice(el.selectionStart);
      el.value = `${before}@${s.label} ${after}`;
      this.composer.prompt = el.value;
      this.mention.open = false;
      el.focus();
    },
    appendMention(label) {
      const sep = this.composer.prompt && !this.composer.prompt.endsWith(" ") ? " " : "";
      this.composer.prompt += `${sep}@${label} `;
    },
    insertMention(ref) { this.appendMention(ref.label); this.$refs.prompt.focus(); },

    // ---- submit a generation ----
    async submit() {
      if (!this.composer.prompt.trim()) return;
      this.submitting = true;
      try {
        const body = {
          prompt: this.composer.prompt.trim(),
          model: this.composer.model,
          resolution: this.composer.resolution,
          num_images: this.composer.num_images,
          reference_image_ids: this.composer.refs.map((r) => r.id),
          system_instruction: (this.composer.useSystem && this.composer.systemInstruction.trim())
            ? this.composer.systemInstruction.trim() : null,
        };
        const g = await api("/api/generations", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        this.mergeGeneration(g);
        this.current = g;
        this.watch(g.id);
        this.$nextTick(() => this.scrollChat());
      } catch (err) {
        this.notify(err.message || "Failed to submit");
      } finally {
        this.submitting = false;
      }
    },

    // ---- library management ----
    async openLibrary(forPicking = false) {
      await this.loadLibrary();
      this.libraryPicking = forPicking;
      this.showLibrary = true;
    },
    async promptSaveToLibrary(img) {
      const name = prompt("Save to library as @…", img.library_name || "");
      if (!name) return;
      try {
        await api(`/api/images/${img.id}/library`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ library_name: name.trim() }),
        });
        this.notify(`Saved as @${name.trim()}`);
        await this.loadLibrary();
      } catch (err) { this.notify(err.message); }
    },
    async removeFromLibrary(img) {
      await api(`/api/images/${img.id}/library`, { method: "DELETE" });
      await this.loadLibrary();
    },
    async deleteImage(img) {
      if (!confirm("Delete this image permanently?")) return;
      await api(`/api/images/${img.id}`, { method: "DELETE" });
      await this.loadLibrary();
    },

    // ---- settings ----
    openSettings() {
      this.settingsDraft = {
        gemini_api_key: this.settings.gemini_api_key || "",
        default_prompt: this.settings.default_prompt || "",
        system_instruction: this.settings.system_instruction || "",
        modelsText: (this.settings.models || []).join(", "),
        default_model: this.settings.default_model || "",
        useBatch: String(this.settings.use_batch) === "true",
        theme: this.settings.theme || "dark",
        useVertex: String(this.settings.use_vertex) === "true",
        gcp_project: this.settings.gcp_project || "",
        gcp_location: this.settings.gcp_location || "global",
      };
      this.showSettings = true;
    },
    async saveSettings() {
      const models = this.settingsDraft.modelsText
        .split(",").map((s) => s.trim()).filter(Boolean);
      await api("/api/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          gemini_api_key: this.settingsDraft.gemini_api_key,
          default_prompt: this.settingsDraft.default_prompt,
          system_instruction: this.settingsDraft.system_instruction,
          models,
          default_model: this.settingsDraft.default_model || models[0] || "",
          use_batch: this.settingsDraft.useBatch ? "true" : "false",
          theme: this.settingsDraft.theme,
          use_vertex: this.settingsDraft.useVertex ? "true" : "false",
          gcp_project: this.settingsDraft.gcp_project.trim(),
          gcp_location: this.settingsDraft.gcp_location.trim() || "global",
        }),
      });
      await this.loadSettings();
      this.showSettings = false;
      this.notify("Settings saved");
    },

    // Save an image to disk via a real "Save As" dialog (File System Access
    // API), falling back to a normal download in browsers that lack it
    // (e.g. Firefox/Safari).
    async saveImage(img) {
      let blob;
      try {
        blob = await (await fetch(img.url)).blob();
      } catch (err) {
        this.notify("Could not load image: " + err.message);
        return;
      }
      const ext = ((blob.type.split("/")[1] || "png").replace("jpeg", "jpg"));
      const suggestedName = `${img.library_name || "image-" + img.id}.${ext}`;

      if (window.showSaveFilePicker) {
        try {
          const handle = await window.showSaveFilePicker({
            suggestedName,
            types: [{ description: "Image",
                      accept: { [blob.type || "image/png"]: ["." + ext] } }],
          });
          const writable = await handle.createWritable();
          await writable.write(blob);
          await writable.close();
          this.notify("Saved");
        } catch (err) {
          if (err.name !== "AbortError") this.notify("Save failed: " + err.message);
        }
      } else {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = suggestedName;
        a.click();
        URL.revokeObjectURL(a.href);
      }
    },

    // ---- image lightbox (click any image to view full-size) ----
    openLightbox(url) { this.lightbox = url; },

    // ---- add an uploaded reference image to the library ----
    async saveRefToLibrary(ref) {
      const name = prompt("Save to library as @…", ref.inLibrary ? ref.label : "");
      if (!name) return;
      try {
        const img = await api(`/api/images/${ref.id}/library`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ library_name: name.trim() }),
        });
        ref.inLibrary = true;
        ref.label = img.library_name;
        this.notify(`Saved as @${img.library_name}`);
      } catch (err) { this.notify(err.message); }
    },

    // ---- theme (persistent) ----
    applyTheme(theme) {
      document.documentElement.setAttribute("data-theme", theme);
    },
    async toggleTheme() {
      const theme = (this.settings.theme === "light") ? "dark" : "light";
      this.settings.theme = theme;
      this.applyTheme(theme);
      try {
        await api("/api/settings", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ theme }),
        });
      } catch (err) { this.notify(err.message); }
    },

    // ---- misc ----
    scrollChat() {
      const el = this.$refs.chat;
      if (el) el.scrollTop = el.scrollHeight;
    },
    notify(msg) {
      this.toast = msg;
      setTimeout(() => { this.toast = ""; }, 3500);
    },
  };
}

// Small fetch wrapper: parses JSON, throws readable errors.
async function api(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}
