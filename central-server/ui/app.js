function app() {
  return {
    page: 'dashboard',
    loading: false,
    error: null,

    stats: {},
    recentInterrogations: [],
    devices: [],
    jobs: [],
    deviceDetail: { serial: null, info: null, interrogations: [] },
    interrDetail: null,
    docPageCount: 0,
    docPageError: false,
    uploadModal: false,
    uploadVendor: 'abbott',
    uploadFile: null,
    uploading: false,

    async init() {
      window.addEventListener('hashchange', () => this.route());
      this.route();
    },

    async route() {
      const hash = window.location.hash.replace('#', '') || '/';
      this.error = null;

      if (hash === '/') {
        this.page = 'dashboard';
        await this.loadDashboard();
      } else if (hash === '/devices') {
        this.page = 'devices';
        await this.loadDevices();
      } else if (hash.startsWith('/devices/')) {
        this.page = 'device-detail';
        const serial = hash.replace('/devices/', '');
        await this.loadDeviceDetail(serial);
      } else if (hash.startsWith('/interrogations/')) {
        this.page = 'interrogation-detail';
        const id = hash.replace('/interrogations/', '');
        await this.loadInterrogation(id);
      } else if (hash === '/jobs') {
        this.page = 'jobs';
        await this.loadJobs();
      }
    },

    async api(path) {
      const res = await fetch(path);
      if (!res.ok) throw new Error(`API error ${res.status} on ${path}`);
      return res.json();
    },

    async loadDashboard() {
      try {
        this.loading = true;
        const [stats, interrs] = await Promise.all([
          this.api('/api/stats'),
          this.api('/api/interrogations?limit=20'),
        ]);
        this.stats = stats;
        this.recentInterrogations = interrs.map(r => this.normalizeInterr(r));
      } catch (e) {
        this.error = e.message;
      } finally {
        this.loading = false;
      }
    },

    async loadDevices() {
      try {
        this.loading = true;
        this.devices = await this.api('/api/devices');
      } catch (e) {
        this.error = e.message;
      } finally {
        this.loading = false;
      }
    },

    async loadDeviceDetail(serial) {
      try {
        this.loading = true;
        this.deviceDetail = { serial, info: null, interrogations: [] };
        const [devices, interrogations] = await Promise.all([
          this.api('/api/devices'),
          this.api(`/api/interrogations?device_serial=${encodeURIComponent(serial)}&limit=100`),
        ]);
        this.deviceDetail.info = devices.find(d => d.device_serial === serial) ?? null;
        this.deviceDetail.interrogations = interrogations.map(r => this.normalizeInterr(r));
      } catch (e) {
        this.error = e.message;
      } finally {
        this.loading = false;
      }
    },

    async loadInterrogation(id) {
      try {
        this.loading = true;
        this.docPageCount = 0;
        this.docPageError = false;
        this.interrDetail = this.normalizeInterr(await this.api(`/api/interrogations/${id}`));
        this._loadDocPages(id);
      } catch (e) {
        this.error = e.message;
      } finally {
        this.loading = false;
      }
    },

    async _loadDocPages(id) {
      try {
        const data = await this.api(`/api/interrogations/${id}/pages`);
        this.docPageCount = data.count || 0;
      } catch (e) {
        this.docPageError = true;
      }
    },

    async loadJobs() {
      try {
        this.loading = true;
        this.jobs = await this.api('/api/jobs?limit=100');
      } catch (e) {
        this.error = e.message;
      } finally {
        this.loading = false;
      }
      this.scheduleJobsRefresh();
    },

    // Map a job's status to a display badge. Falls back to the legacy parsed/error flags.
    statusBadge(j) {
      const s = j.status || (j.parsed ? 'parsed' : (j.parse_error ? 'failed' : 'received'));
      const map = {
        parsed:     { label: 'Parsed',              cls: 'ok' },
        failed:     { label: 'Failed',              cls: 'err' },
        pcl_only:   { label: 'PCL (no renderer)',   cls: 'warn' },
        raw_only:   { label: 'Raw only',            cls: 'pending' },
        extracting: { label: 'Extracting…',         cls: 'pending' },
        queued:     { label: 'Queued',              cls: 'pending' },
        renderable: { label: 'Awaiting extraction', cls: 'pending' },
        received:   { label: 'Received',            cls: 'pending' },
      };
      return map[s] || { label: s, cls: 'pending' };
    },

    // Auto-refresh the jobs list while any job is still in flight (extraction is async).
    scheduleJobsRefresh() {
      const inflight = this.jobs.some(j =>
        ['queued', 'extracting', 'renderable', 'received'].includes(j.status));
      if (this._jobsTimer) clearTimeout(this._jobsTimer);
      if (inflight && this.page === 'jobs') {
        this._jobsTimer = setTimeout(() => {
          if (this.page === 'jobs') this.loadJobs();
        }, 5000);
      }
    },

    // Normalize an interrogation record so interrogation_date is always YYYY-MM-DD.
    normalizeInterr(r) {
      return { ...r, interrogation_date: this.parseToDateInput(r.interrogation_date) || '' };
    },

    // Parse any date string to YYYY-MM-DD for <input type="date"> value.
    // Handles German DD.MM.YYYY, US MM/DD/YYYY, and ISO YYYY-MM-DD.
    parseToDateInput(str) {
      if (!str) return '';
      // German: 16.06.2026 or 16.06.2026 09:28
      const de = str.match(/^(\d{2})\.(\d{2})\.(\d{4})/);
      if (de) return `${de[3]}-${de[2]}-${de[1]}`;
      // US: 06/16/2026
      const us = str.match(/^(\d{2})\/(\d{2})\/(\d{4})/);
      if (us) return `${us[3]}-${us[1]}-${us[2]}`;
      // ISO or already YYYY-MM-DD
      const iso = str.match(/^(\d{4}-\d{2}-\d{2})/);
      if (iso) return iso[1];
      return '';
    },

    todayISO() {
      return new Date().toISOString().slice(0, 10);
    },

    // Save interrogation_date via PATCH and update local record in all lists.
    async patchDate(id, dateValue) {
      if (!dateValue) return;
      try {
        await fetch(`/api/interrogations/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ interrogation_date: dateValue }),
        });
        for (const list of [this.recentInterrogations, this.deviceDetail.interrogations]) {
          const r = list.find(x => x.id === id);
          if (r) r.interrogation_date = dateValue;
        }
        if (this.interrDetail && this.interrDetail.id === id) {
          this.interrDetail.interrogation_date = dateValue;
        }
      } catch (e) {
        this.error = 'Failed to save date: ' + e.message;
      }
    },

    // Clinical colour coding
    batteryClass(v) {
      if (v == null) return '';
      if (v >= 2.8)  return 'val-ok';
      if (v >= 2.5)  return 'val-warn';
      return 'val-critical';
    },

    impedanceClass(v) {
      if (v == null) return '';
      if (v >= 300 && v <= 1000) return 'val-ok';
      if (v >= 200 && v <= 1500) return 'val-warn';
      return 'val-critical';
    },

    fmtDate(str) {
      if (!str) return '—';
      const iso = this.parseToDateInput(str);
      if (iso) {
        return new Date(iso + 'T00:00:00').toLocaleDateString();
      }
      // Fallback: try native Date parsing (handles ISO timestamps from DB)
      const d = new Date(str);
      return isNaN(d) ? str : d.toLocaleString();
    },

    startDetailResize(e, container) {
      const leftEl = container.children[0];
      const startX = (e.touches ? e.touches[0] : e).clientX;
      const startLeftPx = leftEl.getBoundingClientRect().width;

      const move = (ev) => {
        ev.preventDefault();
        const cx = (ev.touches ? ev.touches[0] : ev).clientX;
        const totalW = container.getBoundingClientRect().width;
        const pct = Math.max(15, Math.min(80, (startLeftPx + cx - startX) / totalW * 100));
        container.style.gridTemplateColumns = `${pct.toFixed(1)}% 8px 1fr`;
      };
      const up = () => {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
        document.removeEventListener('touchmove', move);
        document.removeEventListener('touchend', up);
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
      };
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
      document.addEventListener('touchmove', move, { passive: false });
      document.addEventListener('touchend', up);
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    },

    async uploadJob() {
      if (!this.uploadFile) return;
      this.uploading = true;
      try {
        const fd = new FormData();
        fd.append('vendor', this.uploadVendor);
        fd.append('file', this.uploadFile);
        const res = await fetch('/api/upload', { method: 'POST', body: fd });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || res.status);
        }
        const data = await res.json();
        this.uploadModal = false;
        this.uploadFile = null;
        // Reload jobs list and pick up new entry
        await this.loadJobs();
      } catch (e) {
        this.error = 'Upload failed: ' + e.message;
      } finally {
        this.uploading = false;
      }
    },

    async setVendor(id, vendor, job) {
      try {
        const res = await fetch(`/api/jobs/${id}/vendor`, {
          method: 'PATCH',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({vendor}),
        });
        if (!res.ok) throw new Error(`${res.status}`);
        job.vendor = vendor;
      } catch (e) {
        this.error = 'Vendor update failed: ' + e.message;
      }
    },

    async reprocessJob(id) {
      try {
        const res = await fetch(`/api/jobs/${id}/reprocess`, { method: 'POST' });
        if (!res.ok) throw new Error(`${res.status}`);
        const j = this.jobs.find(j => j.id === id);
        if (j) { j.status = 'queued'; j.parsed = 0; j.parse_error = null; }
        // Extraction is async (~1 min/page) — let the auto-refresh pick up the result.
        this.scheduleJobsRefresh();
      } catch (e) {
        this.error = 'Reprocess failed: ' + e.message;
      }
    },

    async deleteJob(id) {
      if (!confirm('Delete this job and its parsed data?')) return;
      try {
        const res = await fetch(`/api/jobs/${id}`, { method: 'DELETE' });
        if (!res.ok) throw new Error(`${res.status}`);
        await this.loadJobs();
      } catch (e) {
        this.error = 'Delete failed: ' + e.message;
      }
    },

    extraFields(r) {
      if (!r || !r.extra) return [];
      try {
        return Object.entries(JSON.parse(r.extra));
      } catch {
        return [];
      }
    },
  };
}
