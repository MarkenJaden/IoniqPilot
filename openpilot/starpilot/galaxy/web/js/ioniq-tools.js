import { reactive } from "../vendor/vue/vue.esm-browser.js"
import { GxDialog } from "./dialog.js"
import { GxNotice } from "./notice.js"
import { GxState } from "./state.js"
import { beginSave, finishSave } from "./save-feedback.js"

export function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`
  if (m > 0) return `${m}m ${String(s).padStart(2, "0")}s`
  return `${s}s`
}

export function formatTimestamp(epochSeconds) {
  if (!epochSeconds) return "—"
  const d = new Date(Number(epochSeconds) * 1000)
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString()
}

export function formatNum(value, digits = 1, suffix = "") {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—"
  return `${Number(value).toFixed(digits)}${suffix}`
}

export function saveCsvDownload(filename, csvText) {
  const blob = new Blob([csvText || ""], { type: "text/csv;charset=utf-8" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = filename || "ioniq-export.csv"
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function saveJsonDownload(filename, jsonText) {
  const blob = new Blob([jsonText || "{}"], { type: "application/json;charset=utf-8" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = filename || "ioniq-discovery.json"
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export const IoniqToolsPage = {
  props: {
    mode: String,
    unauthorized: Function,
    fetcher: { type: Function, default: null },
  },
  components: { GxDialog, GxNotice, GxState },
  setup() {
    return {
      state: reactive({
        data: null,
        error: "",
        notice: "",
        busy: false,
        tab: "fuel",
        pendingConfirm: null,
        inspectedRecording: null,
        snapshotTitle: "Manuelle Diagnose-Aufzeichnung",
        continuousLabel: "Vollaufzeichnung",
        learnLabel: "",
        learnAction: "trigger_event_recording",
        refuelLiters: "",
        refuelFull: true,
        refuelPrice: "",
        refuelCostEur: "",
        refuelOdo: "",
        refuelNotes: "",
        manualTripLabel: "",
        clearTarget: "ALL",
        discoveryLabel: "Verbrauchsanalyse",
        discoveryDashL100: "",
        discoveryDashKm: "",
        discoveryNotes: "",
      }),
      controller: null,
      timer: null,
      generation: 0,
      alive: false,
    }
  },
  watch: {
    mode(nextMode) {
      if (nextMode === "local" && !this.alive) {
        this.alive = true
        this.load()
      } else if (nextMode !== "local" && this.alive) {
        this.alive = false
        this.generation++
        this.controller?.abort()
        clearTimeout(this.timer)
      }
    },
  },
  mounted() {
    if (this.mode === "local") {
      this.alive = true
      this.load()
    }
  },
  beforeUnmount() {
    this.alive = false
    this.generation++
    this.controller?.abort()
    clearTimeout(this.timer)
  },
  computed: {
    pollIntervalMs() {
      const learnState = this.state.data?.buttons?.learnStatus?.state
      if (learnState === "sampling_baseline" || learnState === "listening") return 450
      if (this.state.data?.recordings?.continuousActive || this.state.data?.discovery?.active) return 1500
      return 3000
    },
    diagnostics() { return this.state.data?.diagnostics || {} },
    lastScan() { return this.diagnostics.lastScan || null },
    liveSummary() { return this.diagnostics.liveSummary || {} },
    liveEcus() { return this.diagnostics.liveEcus || [] },
    battery12v() { return this.liveSummary.battery12vHealth || {} },
    thermal() { return this.liveSummary.thermalStatus || {} },
    passiveWarnings() { return this.diagnostics.passive?.activeWarnings || [] },
    warningHistory() { return this.diagnostics.passive?.warningHistory || [] },
    fuel() { return this.state.data?.fuel || {} },
    currentTank() { return this.fuel.currentTank || {} },
    activeTrip() { return this.fuel.activeTrip || null },
    sourceDiag() { return this.fuel.sourceDiagnostics || this.activeTrip?.sourceDiagnostics || {} },
    discovery() { return this.state.data?.discovery || { active: false, captures: [] } },
    learnStatus() { return this.state.data?.buttons?.learnStatus || { state: "idle", detectedPresses: 0, targetPresses: 3 } },
    bindings() { return this.state.data?.buttons?.bindings || [] },
    recordingsList() { return this.state.data?.recordings?.recordings || [] },
  },
  methods: {
    formatDuration,
    formatTimestamp,
    formatNum,
    sourceLabel(mode) {
      return {
        fco_can: "CAN Einspritzung",
        fco_ems14: "CAN Einspritzung (0x545)",
        obd_pid: "OBD-II Live",
        torque_model: "Kennfeld-Modell",
        hev_model: "HEV C-CAN Modell",
        cluster_avg_fci: "Tacho-Referenz",
      }[mode] || mode || "Bereit"
    },
    rawHex(addr) {
      return (this.sourceDiag?.rawFuelFrames || this.liveSummary?.rawFuelFrames || {})[addr] || "—"
    },
    liveEcuFaultCount(ecuId) {
      return this.passiveWarnings.filter((w) => w.ecuId === ecuId).length
    },
    evSplitPct(evKm, iceKm, fallbackTimePct = 0) {
      const total = (Number(evKm) || 0) + (Number(iceKm) || 0)
      if (total > 0.05) return Math.min(100, Math.max(0, Math.round((Number(evKm) || 0) / total * 100)))
      return Math.min(100, Math.max(0, Math.round(Number(fallbackTimePct) || 0)))
    },
    pillClass(status) {
      if (status === "ok" || status === "optimal") return "gx-ioniq__pill gx-ioniq__pill--ok"
      if (status === "warn" || status === "warming" || status === "cold_warmup") return "gx-ioniq__pill gx-ioniq__pill--warn"
      if (status === "critical" || status === "hot_warn") return "gx-ioniq__pill gx-ioniq__pill--danger"
      return "gx-ioniq__pill"
    },
    dtcCardClass(dtc) {
      if (dtc?.active) return "gx-ioniq__dtc gx-ioniq__dtc--active"
      if (dtc?.pending) return "gx-ioniq__dtc gx-ioniq__dtc--pending"
      return "gx-ioniq__dtc gx-ioniq__dtc--historic"
    },
    dtcPillClass(dtc) {
      if (dtc?.active) return "gx-ioniq__pill gx-ioniq__pill--danger"
      if (dtc?.pending) return "gx-ioniq__pill gx-ioniq__pill--warn"
      return "gx-ioniq__pill gx-ioniq__pill--info"
    },
    async request(payload = null) {
      if (!this.alive) return null
      if (payload && this.state.busy) return null
      this.controller?.abort()
      clearTimeout(this.timer)
      const generation = ++this.generation
      const controller = this.controller = new AbortController()
      const timeout = setTimeout(() => controller.abort(), 9000)
      if (payload) this.state.busy = true
      try {
        const endpoint = payload ? "./api/ioniq-tools/action" : "./api/ioniq-tools"
        const doFetch = this.fetcher || ((url, options) => globalThis.fetch(url, options))
        const response = await doFetch(endpoint, {
          method: payload ? "POST" : "GET",
          credentials: "same-origin",
          cache: "no-store",
          signal: controller.signal,
          headers: payload ? { "Content-Type": "application/json" } : {},
          body: payload ? JSON.stringify(payload) : undefined,
        })
        if (!this.alive || generation !== this.generation) return null
        if (response.status === 401) this.unauthorized?.()
        const result = await response.json()
        if (!response.ok) throw new Error(result.error || "Ioniq tools are unavailable")
        this.state.error = ""
        return result
      } catch (error) {
        if (error?.name === "AbortError") return null
        if (this.alive && generation === this.generation) {
          this.state.error = error.message || "Ioniq tools are unavailable"
        }
        return null
      } finally {
        clearTimeout(timeout)
        if (generation === this.generation) {
          this.state.busy = false
          this.controller = null
        }
      }
    },
    async load() {
      const result = await this.request()
      if (result?.version === 1) {
        this.state.data = result
        if (!this.state.refuelPrice && result.config?.fuelPriceEurPerLiter) {
          this.state.refuelPrice = String(result.config.fuelPriceEurPerLiter)
        }
        if (!this.state.refuelOdo && result.diagnostics?.liveSummary?.odometerKm) {
          this.state.refuelOdo = String(result.diagnostics.liveSummary.odometerKm)
        }
        if (!this.state.refuelLiters && result.fuel?.refuelAlert?.estimatedAddedLiters) {
          this.state.refuelLiters = String(result.fuel.refuelAlert.estimatedAddedLiters)
          this.state.refuelFull = Boolean(result.fuel.refuelAlert.isLikelyFullTank ?? true)
          this.onRefuelLitersOrPriceChange()
        }
      }
      if (this.alive) this.timer = setTimeout(() => this.load(), this.pollIntervalMs)
    },
    onRefuelLitersOrPriceChange() {
      const l = parseFloat(String(this.state.refuelLiters).replace(",", "."))
      const p = parseFloat(String(this.state.refuelPrice).replace(",", "."))
      if (!Number.isNaN(l) && l > 0 && !Number.isNaN(p) && p > 0) {
        this.state.refuelCostEur = (l * p).toFixed(2)
      }
    },
    onRefuelCostChange() {
      const l = parseFloat(String(this.state.refuelLiters).replace(",", "."))
      const c = parseFloat(String(this.state.refuelCostEur).replace(",", "."))
      if (!Number.isNaN(l) && l > 0 && !Number.isNaN(c) && c > 0) {
        this.state.refuelPrice = (c / l).toFixed(3)
      }
    },
    prefillFromAlert() {
      const alert = this.fuel.refuelAlert
      if (!alert) return
      this.state.tab = "fuel"
      if (alert.estimatedAddedLiters) {
        this.state.refuelLiters = String(alert.estimatedAddedLiters)
      }
      this.state.refuelFull = Boolean(alert.isLikelyFullTank ?? true)
      this.onRefuelLitersOrPriceChange()
    },
    async act(actionName, fields = {}) {
      if (!this.state.data?.view) return null
      const feedback = beginSave()
      const result = await this.request({ view: this.state.data.view, action: actionName, ...fields })
      const ok = Boolean(result && (result.saved || result.ok))
      finishSave(feedback, ok)
      if (ok) await this.load()
      return result
    },
    confirmAction(actionName, title, message, fields = {}) {
      this.state.pendingConfirm = { action: actionName, title, message, fields }
    },
    cancelConfirm() {
      this.state.pendingConfirm = null
    },
    async runConfirmed() {
      const pending = this.state.pendingConfirm
      this.state.pendingConfirm = null
      if (!pending) return
      await this.act(pending.action, { ...pending.fields, confirmed: true })
    },
    async saveConfigToggle(key, value) {
      await this.act("save_config", { confirmed: true, config: { [key]: value } })
    },
    async triggerSnapshot() {
      const res = await this.act("record_snapshot", { title: this.state.snapshotTitle || "Manuelle Diagnose", immediate: true })
      if (res?.saved) this.state.notice = "Diagnose-Aufzeichnung gespeichert."
    },
    async inspectRecording(recId) {
      const res = await this.act("inspect_recording", { id: recId })
      if (res?.recording) this.state.inspectedRecording = res.recording
    },
    async downloadRecordingCsv(recId) {
      const res = await this.act("export_recording_csv", { id: recId })
      if (res?.csv) saveCsvDownload(res.filename, res.csv)
    },
    async downloadTripsCsv() {
      const res = await this.act("export_trips_csv", {})
      if (res?.csv) saveCsvDownload(res.filename, res.csv)
    },
    async startDiscovery() {
      const res = await this.act("start_can_discovery", {
        label: this.state.discoveryLabel || "Verbrauchsanalyse",
      })
      if (res?.saved) {
        this.state.notice = "CAN-Verbrauchsanalyse gestartet. Nach einigen Minuten auf „Stoppen & auswerten“ tippen."
      }
    },
    async stopDiscovery() {
      const l100Raw = parseFloat(String(this.state.discoveryDashL100 || "").replace(",", "."))
      const kmRaw = parseFloat(String(this.state.discoveryDashKm || "").replace(",", "."))
      const res = await this.act("stop_can_discovery", {
        dashboardLPer100Km: !Number.isNaN(l100Raw) && l100Raw > 0 ? l100Raw : null,
        dashboardDistanceKm: !Number.isNaN(kmRaw) && kmRaw > 0 ? kmRaw : null,
        notes: this.state.discoveryNotes || "",
      })
      if (res?.saved) {
        this.state.discoveryDashL100 = ""
        this.state.discoveryDashKm = ""
        this.state.discoveryNotes = ""
        this.state.notice = "CAN-Analyse gespeichert und Top-Kandidaten berechnet."
      }
    },
    async downloadDiscoveryJson(capId) {
      const res = await this.act("export_can_discovery", { id: capId })
      if (res?.json) saveJsonDownload(res.filename, res.json)
    },
    async startLearn() {
      if (!this.state.learnLabel.trim()) return
      await this.act("start_learn", { label: this.state.learnLabel.trim(), buttonAction: this.state.learnAction })
    },
    async submitRefuel() {
      const liters = parseFloat(String(this.state.refuelLiters).replace(",", "."))
      if (Number.isNaN(liters) || liters <= 0) {
        this.state.error = "Bitte gültige getankte Literzahl eingeben."
        return
      }
      const price = parseFloat(String(this.state.refuelPrice).replace(",", "."))
      const costEur = parseFloat(String(this.state.refuelCostEur).replace(",", "."))
      const odo = this.state.refuelOdo ? parseFloat(String(this.state.refuelOdo).replace(",", ".")) : null
      const res = await this.act("log_refuel", {
        liters,
        fullTank: this.state.refuelFull,
        pricePerLiter: Number.isNaN(price) ? null : price,
        costEur: Number.isNaN(costEur) ? null : costEur,
        odometerKm: Number.isNaN(odo) ? null : odo,
        notes: this.state.refuelNotes,
      })
      if (res?.saved) {
        this.state.refuelLiters = ""
        this.state.refuelCostEur = ""
        this.state.refuelNotes = ""
        this.state.notice = this.state.refuelFull
          ? "Volltanken gespeichert & Tankzyklus + Kalibrierung aktualisiert."
          : "Teiltanken verbucht."
      }
    },
  },
  template: `<section class="gx-driving gx-ioniq" aria-label="Ioniq Diagnostics &amp; Fuel">
    <header class="gx-settings__header gx-page-header">
      <div>
        <h2>Ioniq Diagnostics &amp; Fuel</h2>
        <p>Verbrauchsanalyse, Tankzyklen, Steuergeräte &amp; Fehlercodes (DTC), 3x-Tasten &amp; Event-Recorder.</p>
      </div>
    </header>
    <GxState v-if="mode !== 'local'">Ioniq Diagnostics &amp; Fuel requires the device. No operation was attempted.</GxState>
    <template v-else>
      <GxNotice v-if="state.error" tone="danger">{{ state.error }}</GxNotice>
      <GxState v-if="!state.data && !state.error" loading>Loading Ioniq Diagnostics &amp; Fuel…</GxState>
      <GxState v-else-if="state.data && !state.data.visible" title="Hyundai Ioniq required">Available when connected to a supported Hyundai or Kia vehicle.</GxState>
      <section v-else-if="state.data?.visible" class="gx-card" aria-label="Hyundai Ioniq 2016 Suite">
        <div class="gx-section__header">
          <i class="bi bi-ev-front" aria-hidden="true"></i>
          <span class="gx-section__title">Ioniq Hybrid Suite (2016 AE)</span>
        </div>
        <div class="gx-panel gx-stack">
          <GxNotice v-if="state.notice" tone="success">{{ state.notice }}</GxNotice>
          <div class="gx-ioniq__status-bar">
            <strong>{{ state.data.vehicle?.label }}</strong>
            <span :class="state.data.parked ? 'gx-ioniq__pill gx-ioniq__pill--ok' : 'gx-ioniq__pill gx-ioniq__pill--warn'">
              {{ state.data.parked ? 'Geparkt · Bearbeitbar' : 'In Fahrt · Gesperrt' }}
            </span>
          </div>

          <div class="gx-ioniq__tabs" role="tablist" aria-label="Ioniq Bereiche">
            <button type="button" role="tab" :aria-selected="state.tab === 'fuel'" class="gx-ioniq__tab" @click="state.tab = 'fuel'">
              <i class="bi bi-fuel-pump" aria-hidden="true"></i><span>Sprit &amp; Trips</span>
            </button>
            <button type="button" role="tab" :aria-selected="state.tab === 'dtc'" class="gx-ioniq__tab" @click="state.tab = 'dtc'">
              <i class="bi bi-cpu" aria-hidden="true"></i><span>DTCs &amp; Diagnose</span>
            </button>
            <button type="button" role="tab" :aria-selected="state.tab === 'buttons'" class="gx-ioniq__tab" @click="state.tab = 'buttons'">
              <i class="bi bi-joystick" aria-hidden="true"></i><span>Tasten ({{ bindings.length }})</span>
            </button>
            <button type="button" role="tab" :aria-selected="state.tab === 'recorder'" class="gx-ioniq__tab" @click="state.tab = 'recorder'">
              <i class="bi bi-record-circle" aria-hidden="true"></i><span>Recorder ({{ recordingsList.length }})</span>
            </button>
          </div>

          <GxNotice v-if="fuel.refuelAlert" tone="info">
            <strong>Tankvorgang erkannt:</strong> Tankuhr um +{{ fuel.refuelAlert.deltaBars }} Balken gestiegen
            ({{ formatNum(fuel.refuelAlert.fromPct, 0, '%') }} → {{ formatNum(fuel.refuelAlert.toPct, 0, '%') }},
            ~{{ formatNum(fuel.refuelAlert.estimatedAddedLiters, 1, ' L') }}).
            <div class="gx-actions">
              <button type="button" class="gx-btn" @click="prefillFromAlert()">Werte übernehmen</button>
              <button type="button" class="gx-btn gx-btn--tonal" @click="act('dismiss_refuel_alert')">Ausblenden</button>
            </div>
          </GxNotice>

          <!-- TAB 1: FUEL & TRIPS -->
          <div v-if="state.tab === 'fuel'" class="gx-stack">
            <div class="gx-ioniq__kpis">
              <div class="gx-ioniq__kpi">
                <span class="gx-ioniq__kpi-label">Tank-Ø (berechnet)</span>
                <strong class="gx-ioniq__kpi-val">{{ formatNum(currentTank.estimatedLPer100Km, 2, ' L') }}</strong>
                <span class="gx-ioniq__kpi-sub">pro 100 km · {{ formatNum(currentTank.estimatedFuelLiters, 2, ' L') }}</span>
              </div>
              <div class="gx-ioniq__kpi">
                <span class="gx-ioniq__kpi-label">Tacho-Bordcomputer</span>
                <strong class="gx-ioniq__kpi-val">{{ formatNum(liveSummary.clusterAvgFcL100 ?? liveSummary.clusterAvgFc ?? liveSummary.clusterAvgFcValue ?? activeTrip?.clusterAvgLPer100Km ?? activeTrip?.avgLPer100Km ?? currentTank.estimatedLPer100Km, 1) }}</strong>
                <span class="gx-ioniq__kpi-sub">{{ liveSummary.clusterAvgFcUnitLabel || 'L/100km' }} ({{ (liveSummary.clusterAvgFcL100 ?? liveSummary.clusterAvgFc ?? liveSummary.clusterAvgFcValue) != null ? 'CLU13' : 'HEV-Modell' }})</span>
              </div>
              <div class="gx-ioniq__kpi">
                <span class="gx-ioniq__kpi-label">Tank-Distanz</span>
                <strong class="gx-ioniq__kpi-val">{{ formatNum(currentTank.distanceKm, 1, ' km') }}</strong>
                <span class="gx-ioniq__kpi-sub">{{ currentTank.tripCount || 0 }} Fahrten</span>
              </div>
              <div class="gx-ioniq__kpi">
                <span class="gx-ioniq__kpi-label">Restreichweite</span>
                <strong class="gx-ioniq__kpi-val">{{ liveSummary.dteKm ?? '—' }} km</strong>
                <span class="gx-ioniq__kpi-sub">Tank {{ formatNum(liveSummary.fuelDispPct, 0, '%') }} · {{ formatNum(fuel.calibrationFactor, 3, 'x') }}</span>
              </div>
            </div>

            <div class="gx-ioniq__card">
              <div class="gx-ioniq__bar-wrap">
                <div class="gx-ioniq__bar-head">
                  <span>EV-Anteil im Tank: <strong>{{ evSplitPct(currentTank.evDistanceKm, currentTank.iceDistanceKm) }}%</strong> ({{ formatNum(currentTank.evDistanceKm, 1, ' km') }})</span>
                  <span>Verbrenner: <strong>{{ formatNum(currentTank.iceDistanceKm, 1, ' km') }}</strong></span>
                </div>
                <div class="gx-ioniq__bar" aria-label="EV vs Verbrenner Anteil">
                  <div class="gx-ioniq__bar-ev" :style="{ width: evSplitPct(currentTank.evDistanceKm, currentTank.iceDistanceKm) + '%' }"></div>
                  <div class="gx-ioniq__bar-ice" :style="{ width: (100 - evSplitPct(currentTank.evDistanceKm, currentTank.iceDistanceKm)) + '%' }"></div>
                </div>
              </div>
              <div class="gx-ioniq__bar-wrap">
                <div class="gx-ioniq__bar-head">
                  <span>Füllstand (45 L Tank): <strong>{{ formatNum(liveSummary.fuelDispPct, 0, '%') }}</strong></span>
                  <span>~{{ formatNum((liveSummary.fuelDispPct || 0) * 0.45, 1, ' L') }}</span>
                </div>
                <div class="gx-ioniq__bar">
                  <div class="gx-ioniq__bar-fuel" :style="{ width: Math.min(100, Math.max(0, Number(liveSummary.fuelDispPct) || 0)) + '%' }"></div>
                </div>
              </div>
              <div v-if="fuel.lifetimeTankAvgLPer100Km || (fuel.calibrationFactor && Math.abs(fuel.calibrationFactor - 1.0) > 0.005)" class="gx-ioniq__card-foot">
                <span v-if="fuel.lifetimeTankAvgLPer100Km">Realer Langzeit-Ø: <strong>{{ formatNum(fuel.lifetimeTankAvgLPer100Km, 2, ' L/100km') }}</strong></span>
                <button v-if="fuel.calibrationFactor && Math.abs(fuel.calibrationFactor - 1.0) > 0.005" type="button" class="gx-btn gx-btn--tonal"
                  :disabled="state.busy || !state.data.parked"
                  @click="confirmAction('reset_fuel_calibration', 'Kalibrierung zurücksetzen?', 'Kalibrierfaktor wieder auf 1.000x zurücksetzen?', {})">Kalibrierung auf 1.0x</button>
              </div>
            </div>

            <div v-if="activeTrip" class="gx-ioniq__card gx-ioniq__card--live">
              <div class="gx-ioniq__card-head">
                <span class="gx-ioniq__card-title">Laufende Fahrt</span>
                <span class="gx-ioniq__pill gx-ioniq__pill--ok">LIVE · {{ sourceLabel(activeTrip.sourceMode) }}</span>
              </div>
              <div class="gx-ioniq__stats">
                <div class="gx-ioniq__stat"><span>Distanz</span><strong>{{ formatNum(activeTrip.distanceKm, 2, ' km') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Dauer</span><strong>{{ formatDuration(activeTrip.durationS) }}</strong></div>
                <div class="gx-ioniq__stat"><span>Verbrauch</span><strong>{{ formatNum(activeTrip.fuelLiters, 2, ' L') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Ø Verbrauch</span><strong>{{ formatNum(activeTrip.avgLPer100Km, 2, ' L/100km') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Tacho-Ø</span><strong>{{ formatNum(activeTrip.clusterAvgLPer100Km ?? activeTrip.clusterAvgFci, 1, ' L/100km') }}</strong></div>
                <div class="gx-ioniq__stat"><span>EV-Anteil</span><strong>{{ evSplitPct(activeTrip.evDistanceKm, activeTrip.iceDistanceKm, activeTrip.evTimePct) }}% Strecke · {{ formatNum(activeTrip.evTimePct, 0, '%') }} Zeit</strong></div>
              </div>
              <div class="gx-ioniq__card-foot">
                <span>Geschätzt: {{ formatNum(activeTrip.estimatedCostEur, 2, ' €') }}</span>
                <button type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy" @click="act('end_manual_trip')">Fahrt abschließen</button>
              </div>
            </div>
            <div v-else class="gx-actions">
              <input class="gx-field" v-model="state.manualTripLabel" placeholder="Optionaler Trip-Name" maxlength="64">
              <button type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy" @click="act('start_manual_trip', { label: state.manualTripLabel })">Fahrt manuell starten</button>
            </div>

            <details class="gx-ioniq__card gx-ioniq__details">
              <summary>Verbrauchs-Datenquellen Diagnose ({{ sourceLabel(sourceDiag.activeSource || activeTrip?.sourceMode) }})</summary>
              <div class="gx-ioniq__stats" style="margin-top: 10px">
                <div class="gx-ioniq__stat"><span>Aktive Quelle</span><strong>{{ sourceDiag.activeSource || activeTrip?.sourceMode || 'Bereit' }}</strong></div>
                <div class="gx-ioniq__stat"><span>FCO (EMS20)</span><strong>{{ sourceDiag.fcoValid ? formatNum(sourceDiag.fcoLiters, 3, ' L') : 'Inaktiv' }}</strong></div>
                <div class="gx-ioniq__stat"><span>FCO (EMS14 0x545)</span><strong>{{ sourceDiag.ems14FcoValid ? formatNum(sourceDiag.ems14FcoLiters, 3, ' L') : (sourceDiag.ems14FcoSeen ? 'Prüfung' : 'Inaktiv') }}</strong></div>
                <div class="gx-ioniq__stat"><span>HEV-Modell / Kennfeld</span><strong>{{ formatNum(sourceDiag.modelLiters, 3, ' L') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Momentanfluss</span><strong>{{ formatNum(sourceDiag.instantFuelRateLph ?? activeTrip?.instantFuelRateLph ?? liveSummary.instantFuelRateLph, 2, ' L/h') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Tacho-Fallback</span><strong>{{ formatNum(sourceDiag.clusterFallbackLiters, 3, ' L') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Motordrehzahl</span><strong>{{ formatNum(liveSummary.engineRpm, 0, ' U/min') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Roh-Hex (545/200/50C)</span><strong><code>{{ rawHex('0x545') }}</code> · <code>{{ rawHex('0x200') }}</code> · <code>{{ rawHex('0x50C') }}</code></strong></div>
              </div>
            </details>

            <div class="gx-ioniq__card">
              <div class="gx-ioniq__card-head">
                <span class="gx-ioniq__card-title">CAN-Verbrauchsanalyse (Rohdaten-Mitschnitt)</span>
                <span :class="discovery.active ? 'gx-ioniq__pill gx-ioniq__pill--ok' : 'gx-ioniq__pill'">
                  {{ discovery.active ? ('Aktiv · ' + (discovery.sampleCount || 0) + 's · ' + (discovery.uniqueCanIds || 0) + ' IDs') : 'Bereit' }}
                </span>
              </div>
              <p style="margin:0; font-size:.82rem; color:var(--on-surface-variant)">
                Zeichnet Rohdaten während der Fahrt auf und rankt versteckte 16-Bit Kraftstoffzähler. Danach Tacho-Ø &amp; Distanz eingeben und JSON exportieren.
              </p>
              <template v-if="!discovery.active">
                <div class="gx-actions">
                  <input class="gx-field" v-model="state.discoveryLabel" maxlength="64" placeholder="Name des Mitschnitts">
                  <button type="button" class="gx-btn" :disabled="state.busy" @click="startDiscovery()">Mitschnitt starten</button>
                </div>
              </template>
              <template v-else>
                <div class="gx-ioniq__form-grid">
                  <label class="gx-field-group">Tacho-Ø (L/100km)<input class="gx-field" type="number" step="0.1" min="0.5" max="30" v-model="state.discoveryDashL100" placeholder="z.B. 4.8"></label>
                  <label class="gx-field-group">Tacho-Distanz (km)<input class="gx-field" type="number" step="0.1" min="0.1" max="500" v-model="state.discoveryDashKm" placeholder="optional"></label>
                  <label class="gx-field-group">Notiz<input class="gx-field" v-model="state.discoveryNotes" maxlength="160" placeholder="z.B. Stadt + Landstraße"></label>
                </div>
                <div class="gx-actions">
                  <button type="button" class="gx-btn" :disabled="state.busy" @click="stopDiscovery()">Stoppen &amp; auswerten</button>
                </div>
              </template>
              <div v-for="cap in (discovery.captures || [])" :key="cap.id" class="gx-ioniq__dtc-box">
                <div class="gx-ioniq__card-head">
                  <strong>{{ cap.label }}</strong>
                  <span style="font-size:.78rem; color:var(--text-muted)">{{ formatTimestamp(cap.startedAt) }} · {{ formatDuration(cap.durationS) }} · {{ cap.sampleCount }} Samples · {{ cap.uniqueCanIds }} IDs</span>
                </div>
                <div v-if="cap.topCandidate" style="font-size:.8rem; margin-top:4px">
                  Top-Kandidat: <code>{{ cap.topCandidate.addressHex }}</code> Byte {{ cap.topCandidate.byteOffset }} ({{ cap.topCandidate.endian }})
                  <span v-if="cap.topCandidate.estimatedUlPerTick"> · ~{{ cap.topCandidate.estimatedUlPerTick }} µL/Tick</span>
                </div>
                <div v-else style="font-size:.8rem; color:var(--text-muted); margin-top:4px">Noch kein eindeutiger monotoner 16-Bit Zähler im Abschnitt gefunden.</div>
                <div class="gx-actions" style="margin-top:6px">
                  <button type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy" @click="downloadDiscoveryJson(cap.id)">JSON laden</button>
                  <button type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy || !state.data.parked"
                    @click="confirmAction('delete_can_discovery', 'Mitschnitt löschen?', 'CAN-Verbrauchsanalyse dauerhaft entfernen?', { id: cap.id })">Löschen</button>
                </div>
              </div>
            </div>

            <h4>Letzte Einzelfahrten ({{ (fuel.trips || []).length }})</h4>
            <p v-if="!(fuel.trips || []).length">Noch keine abgeschlossenen Fahrten aufgezeichnet.</p>
            <div v-for="trip in (fuel.trips || []).slice(0, 15)" :key="trip.id" class="gx-ioniq__card">
              <div class="gx-ioniq__card-head">
                <span class="gx-ioniq__card-title">{{ trip.label || formatTimestamp(trip.startedAt) }}</span>
                <span class="gx-ioniq__pill gx-ioniq__pill--info">{{ formatNum(trip.avgLPer100Km, 2, ' L/100km') }}</span>
              </div>
              <div class="gx-ioniq__bar">
                <div class="gx-ioniq__bar-ev" :style="{ width: evSplitPct(trip.evDistanceKm, trip.iceDistanceKm, trip.evTimePct) + '%' }"></div>
                <div class="gx-ioniq__bar-ice" :style="{ width: (100 - evSplitPct(trip.evDistanceKm, trip.iceDistanceKm, trip.evTimePct)) + '%' }"></div>
              </div>
              <div class="gx-ioniq__stats">
                <div class="gx-ioniq__stat"><span>Distanz / Dauer</span><strong>{{ formatNum(trip.distanceKm, 2, ' km') }} · {{ formatDuration(trip.durationS) }}</strong></div>
                <div class="gx-ioniq__stat"><span>Menge / Kosten</span><strong>{{ formatNum(trip.fuelLiters, 2, ' L') }} · {{ formatNum(trip.estimatedCostEur, 2, ' €') }}</strong></div>
                <div class="gx-ioniq__stat"><span>EV / Tacho-Ø</span><strong>{{ evSplitPct(trip.evDistanceKm, trip.iceDistanceKm, trip.evTimePct) }}% ({{ formatNum(trip.evTimePct, 0, '%') }}t) · {{ formatNum(trip.clusterAvgLPer100Km ?? trip.clusterAvgFci ?? trip.avgLPer100Km, 1, ' L') }}</strong></div>
              </div>
              <div class="gx-ioniq__card-foot">
                <span>Quelle: {{ sourceLabel(trip.sourceMode) }}</span>
                <button type="button" class="gx-btn gx-btn--tonal" :disabled="!state.data.parked || state.busy"
                  @click="confirmAction('delete_trip', 'Fahrt löschen?', 'Diese aufgezeichnete Fahrt endgültig entfernen?', { id: trip.id })">Löschen</button>
              </div>
            </div>

            <h4>Tankstopp / Volltanken erfassen</h4>
            <div class="gx-ioniq__card">
              <div class="gx-ioniq__form-grid">
                <label class="gx-field-group">Getankte Liter<input class="gx-field" type="number" step="0.01" min="0.5" max="90" v-model="state.refuelLiters" @input="onRefuelLitersOrPriceChange()" placeholder="z.B. 38.40"></label>
                <label class="gx-field-group">Preis €/L<input class="gx-field" type="number" step="0.001" v-model="state.refuelPrice" @input="onRefuelLitersOrPriceChange()" placeholder="1.759"></label>
                <label class="gx-field-group">Gesamtpreis €<input class="gx-field" type="number" step="0.01" v-model="state.refuelCostEur" @input="onRefuelCostChange()" placeholder="z.B. 67.55"></label>
                <label class="gx-field-group">km-Stand (optional)<input class="gx-field" type="number" step="0.1" v-model="state.refuelOdo"></label>
                <label class="gx-field-group">Notiz<input class="gx-field" v-model="state.refuelNotes" maxlength="120" placeholder="Tankstelle / E10 / E5"></label>
              </div>
              <div class="gx-row gx-row--borderless">
                <span class="gx-row__info gx-row__label">Vollgetankt (Tankzyklus abschließen &amp; kalibrieren)</span>
                <label class="gx-switch"><input type="checkbox" role="switch" v-model="state.refuelFull"><span class="gx-switch__track"></span><span class="gx-switch__thumb"></span></label>
              </div>
              <div class="gx-actions">
                <button type="button" class="gx-btn" :disabled="state.busy || !state.refuelLiters" @click="submitRefuel()">Tanken speichern</button>
                <button type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy" @click="downloadTripsCsv()">Trips CSV</button>
              </div>
            </div>

            <h4>Volltanken-zu-Volltanken Auswertung ({{ (fuel.tankHistory || []).length }})</h4>
            <p v-if="!(fuel.tankHistory || []).length">Nach dem ersten dokumentierten Volltanken erscheint hier der reale Vergleich.</p>
            <div v-for="tank in (fuel.tankHistory || []).slice(0, 10)" :key="tank.id" class="gx-ioniq__card">
              <div class="gx-ioniq__card-head">
                <span class="gx-ioniq__card-title">{{ formatTimestamp(tank.endedAt) }}</span>
                <span class="gx-ioniq__pill gx-ioniq__pill--ok">{{ formatNum(tank.actualLPer100Km, 2, ' L/100km') }}</span>
              </div>
              <div class="gx-ioniq__stats">
                <div class="gx-ioniq__stat"><span>Distanz</span><strong>{{ formatNum(tank.distanceKm, 1, ' km') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Getankt / CAN</span><strong>{{ formatNum(tank.actualLiters, 2, ' L') }} / {{ formatNum(tank.estimatedLiters, 2, ' L') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Kosten</span><strong>{{ formatNum(tank.costEur, 2, ' €') }} ({{ formatNum(tank.costPer100Km, 2, ' €/100km') }})</strong></div>
              </div>
            </div>

            <h4 v-if="(fuel.refuelLog || []).length">Erfasste Tankvorgänge ({{ (fuel.refuelLog || []).length }})</h4>
            <div v-for="rf in (fuel.refuelLog || []).slice(0, 10)" :key="rf.id" class="gx-ioniq__card">
              <div class="gx-ioniq__card-head">
                <span class="gx-ioniq__card-title">{{ formatTimestamp(rf.loggedAt) }}</span>
                <span :class="rf.fullTank ? 'gx-ioniq__pill gx-ioniq__pill--ok' : 'gx-ioniq__pill'">{{ rf.fullTank ? 'Vollgetankt' : 'Teiltankung' }}</span>
              </div>
              <div class="gx-ioniq__stats">
                <div class="gx-ioniq__stat"><span>Menge</span><strong>{{ formatNum(rf.liters, 2, ' L') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Kosten</span><strong>{{ rf.costEur ? formatNum(rf.costEur, 2, ' €') : '—' }}</strong></div>
                <div class="gx-ioniq__stat"><span>km-Stand</span><strong>{{ rf.odometerKm ? formatNum(rf.odometerKm, 1, ' km') : '—' }}</strong></div>
              </div>
              <div class="gx-ioniq__card-foot">
                <span>{{ rf.notes || 'Ohne Notiz' }}</span>
                <button type="button" class="gx-btn gx-btn--tonal" :disabled="!state.data.parked || state.busy"
                  @click="confirmAction('delete_refuel', 'Tankvorgang löschen?', 'Diesen Tank-Eintrag wirklich entfernen?', { id: rf.id })">Löschen</button>
              </div>
            </div>
          </div>

          <!-- TAB 2: ECU & DTC DIAGNOSTICS -->
          <div v-if="state.tab === 'dtc'" class="gx-stack">
            <div class="gx-row gx-row--borderless">
              <span class="gx-row__info gx-row__label">Tempomat-Hauptschalter (Cruise Main) nach Motorstart aktivieren</span>
              <label class="gx-switch"><input type="checkbox" role="switch" :checked="state.data.config?.autoCruiseMain !== false"
                :disabled="state.busy || !state.data.parked" @change="saveConfigToggle('autoCruiseMain', $event.target.checked)"><span class="gx-switch__track"></span><span class="gx-switch__thumb"></span></label>
            </div>
            <div class="gx-row gx-row--borderless">
              <span class="gx-row__info gx-row__label">Automatisches Steuergeräte-Auslesen beim Start</span>
              <label class="gx-switch"><input type="checkbox" role="switch" :checked="Boolean(state.data.config?.scanOnStart)"
                :disabled="state.busy || !state.data.parked" @change="saveConfigToggle('scanOnStart', $event.target.checked)"><span class="gx-switch__track"></span><span class="gx-switch__thumb"></span></label>
            </div>

            <!-- 12V IBS & HEV THERMAL HEALTH CARDS -->
            <div class="gx-ioniq__card">
              <div class="gx-ioniq__card-head">
                <span class="gx-ioniq__card-title">12V-Hilfsbatterie &amp; IBS-Diagnose</span>
                <span :class="pillClass(battery12v.status)">{{ battery12v.label || 'Warte auf BAT11…' }}</span>
              </div>
              <div class="gx-ioniq__stats">
                <div class="gx-ioniq__stat"><span>Spannung</span><strong>{{ formatNum(battery12v.voltageV ?? liveSummary.bat12vVoltageV, 2, ' V') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Ladezustand (SoC)</span><strong>{{ formatNum(battery12v.socPct ?? liveSummary.bat12vSocPct, 0, '%') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Gesundheit (SoH)</span><strong>{{ formatNum(battery12v.sohPct ?? liveSummary.bat12vSohPct, 0, '%') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Strom</span><strong>{{ formatNum(battery12v.currentA ?? liveSummary.bat12vCurrentA, 1, ' A') }}</strong></div>
                <div class="gx-ioniq__stat"><span>LDC-Wandler</span><strong>{{ (battery12v.ldcCharging || battery12v.chargingActive) ? 'Lädt (HV→12V)' : 'Ruhe' }}</strong></div>
              </div>
              <p v-if="battery12v.advice" style="margin:0; font-size:.8rem; color:var(--on-surface-variant)">{{ battery12v.advice }}</p>
            </div>

            <div class="gx-ioniq__card">
              <div class="gx-ioniq__card-head">
                <span class="gx-ioniq__card-title">Hybrid-Thermik &amp; Warmlauf-Monitor</span>
                <span :class="pillClass(thermal.state)">{{ thermal.label || 'Warte auf EMS12…' }}</span>
              </div>
              <div class="gx-ioniq__stats">
                <div class="gx-ioniq__stat"><span>Kühlmittel</span><strong>{{ formatNum(thermal.coolantTempC ?? liveSummary.coolantTempC, 0, ' °C') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Ansaugluft</span><strong>{{ formatNum(thermal.intakeAirTempC ?? thermal.intakeTempC ?? liveSummary.intakeTempC, 0, ' °C') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Außentemp.</span><strong>{{ formatNum(thermal.ambientTempC ?? liveSummary.ambientTempC, 1, ' °C') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Innenraum</span><strong>{{ formatNum(thermal.cabinTempC ?? liveSummary.cabinTempC, 1, ' °C') }}</strong></div>
                <div class="gx-ioniq__stat"><span>Motordrehzahl</span><strong>{{ formatNum(liveSummary.engineRpm, 0, ' U/min') }}</strong></div>
                <div class="gx-ioniq__stat"><span>MIL im Tacho</span><strong :style="{ color: liveSummary.milLamp ? '#fca5a5' : '#6ee7b7' }">{{ liveSummary.milLamp ? 'AKTIV (Gelb)' : 'Aus' }}</strong></div>
              </div>
              <p v-if="thermal.explanation" style="margin:0; font-size:.8rem; color:var(--on-surface-variant)">{{ thermal.explanation }}</p>
            </div>

            <div class="gx-ioniq__card">
              <div class="gx-ioniq__card-head">
                <span class="gx-ioniq__card-title">Live-CAN Echtzeit-Monitor (während der Fahrt aktiv)</span>
                <span class="gx-ioniq__pill gx-ioniq__pill--ok">Passiv · 0% Bus-Eingriff</span>
              </div>
              <p v-if="!liveEcus.length" style="margin:0; font-size:.82rem; color:var(--on-surface-variant)">
                Warte auf C-CAN Frames der Steuergeräte…
              </p>
              <div v-else class="gx-ioniq__ecu-grid">
                <div v-for="ecu in liveEcus" :key="ecu.ecuId" class="gx-ioniq__card" style="padding:10px 12px; gap:6px">
                  <div class="gx-ioniq__card-head">
                    <strong style="font-size:.85rem">{{ ecu.ecuId }} · {{ ecu.name }}</strong>
                    <span :class="!ecu.online ? 'gx-ioniq__pill' : (liveEcuFaultCount(ecu.ecuId) > 0 ? 'gx-ioniq__pill gx-ioniq__pill--danger' : 'gx-ioniq__pill gx-ioniq__pill--ok')">
                      {{ !ecu.online ? 'Warte' : (liveEcuFaultCount(ecu.ecuId) > 0 ? liveEcuFaultCount(ecu.ecuId) + ' Warnung' : 'Online') }}
                    </span>
                  </div>
                  <div style="font-size:.72rem; color:var(--text-muted)">
                    <code>{{ ecu.addressHex }}</code> · {{ ecu.frameCount || 0 }} Frames
                  </div>
                </div>
              </div>
            </div>

            <h4>Aktiver UDS/OBD-II Tiefenscan (Startfenster)</h4>
            <p style="margin:0; font-size:.82rem; color:var(--on-surface-variant)">
              Aktive UDS/OBD-II Abfragen laufen sicher im kurzen ELM327-Startfenster beim Zündung-EIN (wenn „beim Start auslesen“ aktiv oder Scan vorgemerkt ist), ohne das LKAS-Relais während der Fahrt zu unterbrechen.
            </p>
            <div class="gx-actions">
              <button type="button" class="gx-btn" :disabled="state.busy" @click="act('scan_dtcs')">Steuergeräte scannen</button>
              <select class="gx-field" v-model="state.clearTarget">
                <option value="ALL">Alle Steuergeräte (ALL)</option>
                <option v-for="ecu in state.data.ecuCatalog" :key="ecu.ecuId" :value="ecu.ecuId">{{ ecu.ecuId }} ({{ ecu.txHex }})</option>
              </select>
              <button type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy || !state.data.parked"
                @click="confirmAction('clear_dtcs', 'Fehlercodes wirklich löschen?', 'Ausgewählte DTCs werden im Steuergerät gelöscht.', { target: state.clearTarget })">DTCs löschen</button>
              <button v-if="passiveWarnings.length || warningHistory.length" type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy"
                @click="act('clear_passive_warnings')">Warnungen zurücksetzen</button>
            </div>

            <GxNotice v-if="diagnostics.pendingAction" tone="info">
              Vorgemerkt für nächstes ELM327-Diagnosefenster (Zündung EIN, Motor aus): {{ diagnostics.pendingAction.type.toUpperCase() }} ({{ diagnostics.pendingAction.target }})
              <button type="button" class="gx-btn gx-btn--tonal" @click="act('cancel_pending_diag')">Abbrechen</button>
            </GxNotice>

            <template v-for="w in passiveWarnings" :key="w.code">
              <GxNotice tone="warning">[Aktive Live-CAN Warnung · {{ w.ecuId }}] {{ w.message }}</GxNotice>
            </template>

            <template v-if="lastScan">
              <div class="gx-ioniq__card">
                <div class="gx-ioniq__card-head">
                  <span class="gx-ioniq__card-title">Diagnose-Zusammenfassung</span>
                  <span :class="(lastScan.analysis?.activeCount || 0) > 0 ? 'gx-ioniq__pill gx-ioniq__pill--danger' : (lastScan.analysis?.pendingCount || 0) > 0 ? 'gx-ioniq__pill gx-ioniq__pill--warn' : 'gx-ioniq__pill gx-ioniq__pill--ok'">
                    {{ lastScan.analysis?.overallUrgencyLabel || 'Unauffällig' }}
                  </span>
                </div>
                <div class="gx-ioniq__stats">
                  <div class="gx-ioniq__stat"><span>Scan-Zeitpunkt</span><strong>{{ formatTimestamp(lastScan.scannedAt) }}</strong></div>
                  <div class="gx-ioniq__stat"><span>Steuergeräte</span><strong>{{ lastScan.ecusResponded }}/{{ lastScan.ecusTotal }} online</strong></div>
                  <div class="gx-ioniq__stat"><span>Fehlercodes</span><strong>{{ lastScan.analysis?.activeCount || 0 }} Aktiv · {{ lastScan.analysis?.pendingCount || 0 }} Ausst. · {{ lastScan.analysis?.historicCount || 0 }} Hist.</strong></div>
                  <div v-if="lastScan.durationMs" class="gx-ioniq__stat"><span>Scan-Dauer</span><strong>{{ lastScan.durationMs }} ms</strong></div>
                </div>
                <div v-if="lastScan.readiness" class="gx-ioniq__dtc-box">
                  <strong>OBD-II Readiness (0x7E0):</strong> MIL {{ lastScan.readiness.milOn ? 'AN' : 'Aus' }} ·
                  Abgas-DTCs: {{ lastScan.readiness.emissionDtcCount }} ·
                  Offene Tests: {{ (lastScan.readiness.incompleteMonitors || []).length ? lastScan.readiness.incompleteMonitors.join(', ') : 'Alle abgeschlossen' }}
                </div>
                <div v-if="(lastScan.obdSupportedPids || []).length" class="gx-ioniq__dtc-box">
                  <strong>Unterstützte OBD-II PIDs (0x7E0):</strong> {{ lastScan.obdSupportedPids.join(', ') }}
                </div>
                <div v-if="lastScan.freezeFrame" class="gx-ioniq__dtc-box">
                  <strong>OBD Freeze-Frame ({{ lastScan.freezeFrame.dtcCode }}):</strong>
                  {{ formatNum(lastScan.freezeFrame.rpm, 0, ' U/min') }} ·
                  {{ formatNum(lastScan.freezeFrame.speedKph, 0, ' km/h') }} ·
                  Kühlmittel {{ formatNum(lastScan.freezeFrame.coolantTempC, 0, ' °C') }} ·
                  Last {{ formatNum(lastScan.freezeFrame.engineLoadPct, 1, '%') }}
                </div>
                <p v-if="lastScan.analysis?.milExplanation" style="margin:0; font-size:.82rem; color:var(--on-surface-variant)">{{ lastScan.analysis.milExplanation }}</p>
              </div>

              <div v-for="ins in (lastScan.analysis?.insights || [])" :key="ins.title" class="gx-ioniq__card">
                <div class="gx-ioniq__card-title">{{ ins.title }}</div>
                <div style="font-size:.84rem; color:var(--on-surface-variant)">{{ ins.detail }}</div>
              </div>

              <h4>Fehlercode-Analyse ({{ (lastScan.dtcs || []).length }})</h4>
              <GxNotice v-if="!(lastScan.dtcs || []).length" tone="success">Keine gespeicherten Fehlercodes im letzten Scan gefunden.</GxNotice>
              <div v-for="dtc in (lastScan.dtcs || [])" :key="dtc.ecuId + dtc.code + dtc.source" :class="dtcCardClass(dtc)">
                <div class="gx-ioniq__card-head">
                  <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap">
                    <span class="gx-ioniq__dtc-code">{{ dtc.code }}</span>
                    <span class="gx-ioniq__pill">{{ dtc.ecuId }} · {{ dtc.system }}</span>
                  </div>
                  <span :class="dtcPillClass(dtc)">
                    {{ dtc.active ? 'AKTIV' : dtc.pending ? 'AUSSTEHEND' : 'HISTORISCH' }} · {{ dtc.urgencyLabel || 'Prüfen' }}
                  </span>
                </div>
                <div class="gx-ioniq__card-title">{{ dtc.title }}</div>
                <div v-if="dtc.stateExplanation" style="font-size:.82rem; color:var(--on-surface-variant)">{{ dtc.stateExplanation }}</div>
                <div class="gx-ioniq__dtc-box">
                  <strong>Werkstatt- &amp; Handlungshinweis:</strong> {{ dtc.workshopAdvice || dtc.hint }}
                </div>
                <div v-if="(dtc.causes || []).length" class="gx-stack" style="gap:6px">
                  <span style="font-size:.75rem; color:var(--text-muted)">Typische Ursachen beim 2016 Ioniq Hybrid:</span>
                  <div class="gx-ioniq__causes">
                    <span v-for="c in dtc.causes" :key="c" class="gx-ioniq__pill">{{ c }}</span>
                  </div>
                </div>
                <div v-if="dtc.freezeFrame" class="gx-ioniq__stats">
                  <div class="gx-ioniq__stat"><span>Drehzahl</span><strong>{{ formatNum(dtc.freezeFrame.rpm, 0, ' U/min') }}</strong></div>
                  <div class="gx-ioniq__stat"><span>Tempo</span><strong>{{ formatNum(dtc.freezeFrame.speedKph, 0, ' km/h') }}</strong></div>
                  <div class="gx-ioniq__stat"><span>Kühlmittel / Last</span><strong>{{ formatNum(dtc.freezeFrame.coolantTempC, 0, ' °C') }} · {{ formatNum(dtc.freezeFrame.engineLoadPct, 1, '%') }}</strong></div>
                </div>
                <div v-if="dtc.snapshotHex" style="font-size:.75rem; color:var(--text-muted)">
                  UDS Snapshot: <code>{{ dtc.snapshotHex }}</code>
                </div>
              </div>

              <h4>Steuergeräte Übersicht ({{ (lastScan.ecus || []).length }})</h4>
              <div class="gx-ioniq__ecu-grid">
                <div v-for="ecu in (lastScan.ecus || [])" :key="ecu.ecuId" class="gx-ioniq__card" style="padding:10px 12px; gap:6px">
                  <div class="gx-ioniq__card-head">
                    <strong style="font-size:.85rem">{{ ecu.ecuId }} · {{ ecu.name }}</strong>
                    <span :class="ecu.online ? (ecu.dtcCount > 0 ? 'gx-ioniq__pill gx-ioniq__pill--warn' : 'gx-ioniq__pill gx-ioniq__pill--ok') : 'gx-ioniq__pill'">
                      {{ ecu.online ? (ecu.dtcCount > 0 ? ecu.dtcCount + ' DTC' : 'OK') : 'Offline' }}
                    </span>
                  </div>
                  <div style="font-size:.72rem; color:var(--text-muted)">
                    {{ ecu.txHex }}/{{ ecu.rxHex }}
                    <span v-if="ecu.online && ecu.busUsed !== null && ecu.busUsed !== undefined"> · {{ ecu.busUsed === 0 ? 'C-CAN' : 'OBD-II' }}</span>
                    <span v-if="ecu.partNumber"> · {{ ecu.partNumber }}</span>
                    <span v-if="ecu.softwareVersion"> · SW {{ ecu.softwareVersion }}</span>
                  </div>
                </div>
              </div>
            </template>
            <div v-else class="gx-ioniq__card">
              <strong>Noch kein aktiver ECU-Scan ausgeführt</strong>
              <p style="margin:0; font-size:.84rem; color:var(--on-surface-variant)">
                Tippe oben im Stand (Zündung EIN) auf „Steuergeräte scannen“, um alle 14 Steuergeräte inkl. Freeze-Frame und Fehlerdetails auszulesen.
              </p>
            </div>
          </div>

          <!-- TAB 3: 3X BUTTON LEARNER -->
          <div v-if="state.tab === 'buttons'" class="gx-stack">
            <p style="margin:0; font-size:.84rem; color:var(--on-surface-variant)">Drücke eine Taste im Auto 3x hintereinander. Zähler und Prüfsummen werden automatisch gefiltert.</p>
            <div class="gx-ioniq__form-grid">
              <label class="gx-field-group">Name der Taste<input class="gx-field" v-model="state.learnLabel" maxlength="48" placeholder="z.B. EV-Knopf oder Lenkrad-Stern"></label>
              <label class="gx-field-group">Aktion zuweisen
                <select class="gx-field" v-model="state.learnAction">
                  <option v-for="actItem in (state.data.buttonActions || [])" :key="actItem.id" :value="actItem.id">{{ actItem.label }}</option>
                </select>
              </label>
            </div>
            <div class="gx-actions">
              <button type="button" class="gx-btn" :disabled="state.busy || !state.data.parked || !state.learnLabel.trim()" @click="startLearn()">3x-Anlernmodus starten</button>
              <button v-if="learnStatus.state === 'sampling_baseline' || learnStatus.state === 'listening'" type="button" class="gx-btn gx-btn--tonal" @click="act('cancel_learn')">Abbrechen</button>
            </div>
            <GxNotice :tone="learnStatus.state === 'learned' ? 'success' : learnStatus.state === 'timeout' ? 'warning' : 'info'">
              Status ({{ learnStatus.state }} · {{ learnStatus.detectedPresses || 0 }}/{{ learnStatus.targetPresses || 3 }}): {{ learnStatus.message }}
            </GxNotice>

            <h4>Gelernte Tasten ({{ bindings.length }})</h4>
            <p v-if="!bindings.length">Noch keine Tasten gelernt.</p>
            <div v-for="b in bindings" :key="b.id" class="gx-ioniq__card">
              <div class="gx-ioniq__card-head">
                <span class="gx-ioniq__card-title">{{ b.name }}</span>
                <span class="gx-ioniq__pill">Bus {{ b.bus }} · {{ b.addressHex }}</span>
              </div>
              <div style="font-size:.78rem; color:var(--text-muted)">Byte {{ b.byteIndex }} · Maske {{ b.bitMaskHex }} ({{ b.mode }}) · Auslösungen: {{ b.triggerCount || 0 }}</div>
              <div class="gx-actions">
                <select class="gx-field" :value="b.action" :disabled="!state.data.parked" @change="act('update_binding', { id: b.id, buttonAction: $event.target.value })">
                  <option v-for="actItem in (state.data.buttonActions || [])" :key="actItem.id" :value="actItem.id">{{ actItem.label }}</option>
                </select>
                <label class="gx-switch"><input type="checkbox" role="switch" :checked="b.enabled" :disabled="!state.data.parked"
                  @change="act('update_binding', { id: b.id, enabled: $event.target.checked })"><span class="gx-switch__track"></span><span class="gx-switch__thumb"></span></label>
                <button type="button" class="gx-btn gx-btn--tonal" :disabled="!state.data.parked"
                  @click="confirmAction('delete_binding', 'Taste entfernen?', 'Gelernte CAN-Taste dauerhaft löschen?', { id: b.id })">Löschen</button>
              </div>
            </div>
          </div>

          <!-- TAB 4: EVENT & TELEMETRY RECORDER -->
          <div v-if="state.tab === 'recorder'" class="gx-stack">
            <div class="gx-row gx-row--borderless">
              <span class="gx-row__info gx-row__label">Automatische Aufzeichnung bei echten CAN-Fehlern</span>
              <label class="gx-switch"><input type="checkbox" role="switch" :checked="state.data.config?.recorderAutoOnFault"
                :disabled="state.busy || !state.data.parked" @change="saveConfigToggle('recorderAutoOnFault', $event.target.checked)"><span class="gx-switch__track"></span><span class="gx-switch__thumb"></span></label>
            </div>
            <div class="gx-ioniq__status-bar">
              <span style="font-size:.82rem; color:var(--on-surface-variant)">Ringpuffer: {{ state.data.recordings?.ringBufferSamples || 0 }} Samples</span>
              <span :class="state.data.recordings?.continuousActive ? 'gx-ioniq__pill gx-ioniq__pill--danger' : 'gx-ioniq__pill'">
                {{ state.data.recordings?.continuousActive ? 'Vollaufzeichnung LÄUFT' : 'Inaktiv' }}
              </span>
            </div>
            <div class="gx-actions">
              <input class="gx-field" v-model="state.snapshotTitle" maxlength="80" placeholder="Titel für Momentaufnahme">
              <button type="button" class="gx-btn" :disabled="state.busy" @click="triggerSnapshot()">Momentaufnahme</button>
              <button v-if="!state.data.recordings?.continuousActive" type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy" @click="act('start_continuous_recording', { label: state.continuousLabel })">Vollaufzeichnung</button>
              <button v-else type="button" class="gx-btn" :disabled="state.busy" @click="act('stop_continuous_recording')">Stoppen</button>
            </div>

            <div v-for="rec in recordingsList" :key="rec.id" class="gx-ioniq__card">
              <div class="gx-ioniq__card-head">
                <span class="gx-ioniq__card-title">{{ rec.title }}</span>
                <span class="gx-ioniq__pill">{{ rec.reason }}</span>
              </div>
              <div class="gx-ioniq__stats">
                <div class="gx-ioniq__stat"><span>Zeitpunkt</span><strong>{{ formatTimestamp(rec.startedAt) }}</strong></div>
                <div class="gx-ioniq__stat"><span>Dauer / Samples</span><strong>{{ formatDuration(rec.durationS) }} · {{ rec.sampleCount }}</strong></div>
                <div class="gx-ioniq__stat"><span>Max</span><strong>{{ rec.maxSpeedKph }} km/h · {{ rec.maxRpm }} U/min</strong></div>
              </div>
              <div class="gx-actions">
                <button type="button" class="gx-btn gx-btn--tonal" @click="inspectRecording(rec.id)">Details</button>
                <button type="button" class="gx-btn gx-btn--tonal" @click="downloadRecordingCsv(rec.id)">CSV</button>
                <button type="button" class="gx-btn gx-btn--tonal" :disabled="!state.data.parked"
                  @click="confirmAction('delete_recording', 'Aufzeichnung löschen?', 'Diagnose-Datei dauerhaft löschen?', { id: rec.id })">Löschen</button>
              </div>
            </div>

            <div v-if="state.inspectedRecording" class="gx-ioniq__card">
              <div class="gx-ioniq__card-head">
                <strong>Aufzeichnung: {{ state.inspectedRecording.title }}</strong>
                <button type="button" class="gx-btn gx-btn--tonal" @click="state.inspectedRecording = null">Schließen</button>
              </div>
              <div v-for="ev in (state.inspectedRecording.events || [])" :key="ev.t + ev.message" style="font-size:.8rem">
                {{ formatTimestamp(ev.t) }} [{{ ev.kind }}]: {{ ev.message }}
              </div>
              <p style="margin:0; font-size:.78rem; color:var(--text-muted)">Telemetrie-Zeilen: {{ (state.inspectedRecording.samples || []).length }} (CSV-Export enthält alle Werte).</p>
            </div>
          </div>

          <GxDialog v-if="state.pendingConfirm" labelledby="gx-ioniq-confirm" @close="cancelConfirm()">
            <h3 id="gx-ioniq-confirm">{{ state.pendingConfirm.title }}</h3>
            <p>{{ state.pendingConfirm.message }}</p>
            <div class="gx-actions">
              <button type="button" class="gx-btn gx-btn--tonal" @click="cancelConfirm()">Abbrechen</button>
              <button type="button" class="gx-btn" @click="runConfirmed()">Bestätigen</button>
            </div>
          </GxDialog>
        </div>
      </section>
    </template>
  </section>`,
}
