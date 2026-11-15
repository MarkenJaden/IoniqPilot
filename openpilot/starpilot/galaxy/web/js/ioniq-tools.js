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
      if (this.state.data?.recordings?.continuousActive) return 1500
      return 3000
    },
    diagnostics() { return this.state.data?.diagnostics || {} },
    lastScan() { return this.diagnostics.lastScan || null },
    liveSummary() { return this.diagnostics.liveSummary || {} },
    battery12v() { return this.liveSummary.battery12vHealth || {} },
    thermal() { return this.liveSummary.thermalStatus || {} },
    passiveWarnings() { return this.diagnostics.passive?.activeWarnings || [] },
    warningHistory() { return this.diagnostics.passive?.warningHistory || [] },
    fuel() { return this.state.data?.fuel || {} },
    currentTank() { return this.fuel.currentTank || {} },
    activeTrip() { return this.fuel.activeTrip || null },
    sourceDiag() { return this.fuel.sourceDiagnostics || this.activeTrip?.sourceDiagnostics || {} },
    learnStatus() { return this.state.data?.buttons?.learnStatus || { state: "idle", detectedPresses: 0, targetPresses: 3 } },
    bindings() { return this.state.data?.buttons?.bindings || [] },
    recordingsList() { return this.state.data?.recordings?.recordings || [] },
  },
  methods: {
    formatDuration,
    formatTimestamp,
    formatNum,
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
  template: `<section class="gx-driving" aria-label="Ioniq Diagnostics &amp; Fuel">
    <header class="gx-settings__header gx-page-header">
      <div>
        <h2>Ioniq Diagnostics &amp; Fuel</h2>
        <p>Fuel consumption, tank cycles, ECU fault codes (DTC), 3x CAN buttons and event recorder.</p>
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
          <span class="gx-section__title">Ioniq Hybrid Custom Suite (2016 AE)</span>
        </div>
        <div class="gx-panel gx-stack">
          <GxNotice v-if="state.notice" tone="success">{{ state.notice }}</GxNotice>
          <p>{{ state.data.vehicle?.label }} · Status: {{ state.data.parked ? 'Geparkt' : 'In Fahrt (aktive Änderungen gesperrt)' }}</p>
          <div class="gx-actions">
            <button type="button" :class="state.tab === 'fuel' ? 'gx-btn' : 'gx-btn gx-btn--tonal'" @click="state.tab = 'fuel'">Sprit &amp; Trips</button>
            <button type="button" :class="state.tab === 'dtc' ? 'gx-btn' : 'gx-btn gx-btn--tonal'" @click="state.tab = 'dtc'">Steuergeräte &amp; DTCs</button>
            <button type="button" :class="state.tab === 'buttons' ? 'gx-btn' : 'gx-btn gx-btn--tonal'" @click="state.tab = 'buttons'">3x-Knopf Lernen ({{ bindings.length }})</button>
            <button type="button" :class="state.tab === 'recorder' ? 'gx-btn' : 'gx-btn gx-btn--tonal'" @click="state.tab = 'recorder'">Aufzeichnung ({{ recordingsList.length }})</button>
          </div>

          <GxNotice v-if="fuel.refuelAlert" tone="info">
            <strong>Tankvorgang automatisch erkannt:</strong> Tankuhr um +{{ fuel.refuelAlert.deltaBars }} Balken gestiegen
            (von {{ formatNum(fuel.refuelAlert.fromPct, 0, '%') }} auf {{ formatNum(fuel.refuelAlert.toPct, 0, '%') }},
            geschätzt ~{{ formatNum(fuel.refuelAlert.estimatedAddedLiters, 1, ' L') }}).
            Bitte tatsächliche Literzahl und Preis eintragen / bestätigen:
            <div class="gx-actions">
              <button type="button" class="gx-btn" @click="prefillFromAlert()">Werte übernehmen</button>
              <button type="button" class="gx-btn gx-btn--tonal" @click="act('dismiss_refuel_alert')">Ausblenden</button>
            </div>
          </GxNotice>

          <!-- TAB 1: FUEL & TRIPS -->
          <div v-if="state.tab === 'fuel'" class="gx-stack">
            <h4>Aktueller Tankzyklus (Volltanken zu Volltanken)</h4>
            <p>
              Distanz: <strong>{{ formatNum(currentTank.distanceKm, 1, ' km') }}</strong> ·
              Berechnet: <strong>{{ formatNum(currentTank.estimatedFuelLiters, 2, ' L') }}</strong>
              ({{ formatNum(currentTank.estimatedLPer100Km, 2, ' L/100km') }}) ·
              EV-Strecke: <strong>{{ formatNum(currentTank.evDistanceKm, 1, ' km') }}</strong> ·
              Verbrenner: <strong>{{ formatNum(currentTank.iceDistanceKm, 1, ' km') }}</strong> ·
              Fahrten: <strong>{{ currentTank.tripCount || 0 }}</strong>
            </p>
            <p>
              Tacho-Bordcomputer (CLU13): <strong>{{ formatNum(liveSummary.clusterAvgFc, 1, ' ' + (liveSummary.clusterAvgFcUnit || 'L/100km')) }}</strong> ·
              Tankuhr: <strong>{{ formatNum(liveSummary.fuelDispPct, 0, '%') }}</strong>
              (~{{ formatNum((liveSummary.fuelDispPct || 0) * 0.45, 1, ' L') }} von 45 L · Restreichweite {{ liveSummary.dteKm ?? '—' }} km) ·
              Kalibrierung: <strong>{{ formatNum(fuel.calibrationFactor, 3, 'x') }}</strong>
              <button v-if="fuel.calibrationFactor && Math.abs(fuel.calibrationFactor - 1.0) > 0.005" type="button" class="gx-btn gx-btn--tonal"
                :disabled="state.busy || !state.data.parked"
                @click="confirmAction('reset_fuel_calibration', 'Kalibrierung zurücksetzen?', 'Kalibrierfaktor wieder auf 1.000x zurücksetzen?', {})">Auf 1.0x zurücksetzen</button>
            </p>
            <p v-if="fuel.lifetimeTankAvgLPer100Km">
              Realer Gesamtdurchschnitt (aus Volltankungen): <strong>{{ formatNum(fuel.lifetimeTankAvgLPer100Km, 2, ' L/100km') }}</strong>
            </p>

            <div class="gx-panel gx-stack">
              <strong>Verbrauchs-Datenquellen Diagnose (Live-CAN)</strong>
              <p>
                Aktive Quelle: <strong>{{ sourceDiag.activeSource || activeTrip?.sourceMode || 'Bereit' }}</strong> ·
                FCO-Zähler (EMS20): {{ sourceDiag.fcoValid ? 'Aktiv (' + formatNum(sourceDiag.fcoLiters, 3, ' L') + ')' : 'Nicht aktiv / Modell-Fallback' }} ·
                Motor-Kennfeld (EMS11/H12): {{ formatNum(sourceDiag.modelLiters, 3, ' L') }} (aktuell {{ formatNum(liveSummary.instantFuelRateLph, 2, ' L/h') }}) ·
                Bordcomputer-Fallback (CLU13): {{ formatNum(sourceDiag.clusterFallbackLiters, 3, ' L') }}
              </p>
            </div>

            <div v-if="activeTrip" class="gx-panel gx-stack">
              <strong>Laufende Fahrt (Zündung EIN → Zündung AUS)</strong>
              <p>
                Dauer: {{ formatDuration(activeTrip.durationS) }} ·
                Distanz: {{ formatNum(activeTrip.distanceKm, 2, ' km') }} ·
                Verbrauch: <strong>{{ formatNum(activeTrip.fuelLiters, 2, ' L') }}</strong>
                (<strong>{{ formatNum(activeTrip.avgLPer100Km, 2, ' L/100km') }}</strong> · {{ formatNum(activeTrip.estimatedCostEur, 2, ' €') }}) ·
                Tacho-Ø: {{ formatNum(activeTrip.clusterAvgLPer100Km ?? activeTrip.clusterAvgFci, 1, ' L/100km') }} ·
                EV-Anteil: {{ formatNum(activeTrip.evTimePct, 0, '% Zeit') }} / {{ formatNum(activeTrip.evDistancePct, 0, '% Strecke') }} ·
                Quelle: {{ activeTrip.sourceMode }}
              </p>
              <div class="gx-actions">
                <button type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy" @click="act('end_manual_trip')">Aktive Fahrt manuell abschließen</button>
              </div>
            </div>
            <div v-else class="gx-actions">
              <input class="gx-field" v-model="state.manualTripLabel" placeholder="Optionaler Trip-Name" maxlength="64">
              <button type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy" @click="act('start_manual_trip', { label: state.manualTripLabel })">Fahrt manuell starten</button>
            </div>

            <h4>Tankstopp / Volltanken erfassen &amp; Kostenanalyse</h4>
            <div class="gx-actions">
              <label class="gx-field-group">Getankte Liter<input class="gx-field" type="number" step="0.01" min="0.5" max="90" v-model="state.refuelLiters" @input="onRefuelLitersOrPriceChange()" placeholder="z.B. 38.40"></label>
              <label class="gx-field-group">Preis €/L<input class="gx-field" type="number" step="0.001" v-model="state.refuelPrice" @input="onRefuelLitersOrPriceChange()" placeholder="1.759"></label>
              <label class="gx-field-group">Gesamtpreis €<input class="gx-field" type="number" step="0.01" v-model="state.refuelCostEur" @input="onRefuelCostChange()" placeholder="z.B. 67.55"></label>
              <label class="gx-field-group">km-Stand (optional)<input class="gx-field" type="number" step="0.1" v-model="state.refuelOdo"></label>
              <label class="gx-field-group">Notiz<input class="gx-field" v-model="state.refuelNotes" maxlength="120" placeholder="Tankstelle / E10 / E5"></label>
            </div>
            <div class="gx-row gx-row--borderless">
              <span class="gx-row__info gx-row__label">Vollgetankt (Tankzyklus abschließen, realen L/100km &amp; €/100km berechnen und CAN-Sensorik kalibrieren)</span>
              <label class="gx-switch"><input type="checkbox" role="switch" v-model="state.refuelFull"><span class="gx-switch__track"></span><span class="gx-switch__thumb"></span></label>
            </div>
            <div class="gx-actions">
              <button type="button" class="gx-btn" :disabled="state.busy || !state.refuelLiters" @click="submitRefuel()">Tanken speichern</button>
              <button type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy" @click="downloadTripsCsv()">Trips CSV exportieren</button>
            </div>

            <h4>Erfasste Tankvorgänge ({{ (fuel.refuels || []).length }})</h4>
            <p v-if="!(fuel.refuels || []).length">Noch keine Tankvorgänge eingetragen.</p>
            <div v-for="rf in (fuel.refuels || []).slice(0, 10)" :key="rf.id" class="gx-row">
              <div class="gx-row__info">
                <div class="gx-row__label">
                  {{ formatTimestamp(rf.timestamp) }} — {{ formatNum(rf.liters, 2, ' L') }}
                  ({{ rf.fullTank ? 'Vollgetankt' : 'Teiltankung' }})
                  <span v-if="rf.costEur"> · {{ formatNum(rf.costEur, 2, ' €') }} ({{ formatNum(rf.pricePerLiter, 3, ' €/L') }})</span>
                </div>
                <div>
                  km-Stand: {{ rf.odometerKm ? formatNum(rf.odometerKm, 1, ' km') : '—' }}
                  <span v-if="rf.notes"> · {{ rf.notes }}</span>
                </div>
              </div>
              <button type="button" class="gx-btn gx-btn--tonal" :disabled="!state.data.parked || state.busy"
                @click="confirmAction('delete_refuel', 'Tankvorgang löschen?', 'Diesen Tank-Eintrag wirklich entfernen?', { id: rf.id })">Löschen</button>
            </div>

            <h4>Volltanken-zu-Volltanken Auswertung ({{ (fuel.tankHistory || []).length }})</h4>
            <div v-for="tank in (fuel.tankHistory || []).slice(0, 10)" :key="tank.id" class="gx-row">
              <div class="gx-row__info">
                <div class="gx-row__label">
                  {{ formatTimestamp(tank.endedAt) }} — Real: <strong>{{ formatNum(tank.actualLPer100Km, 2, ' L/100km') }}</strong>
                  <span v-if="tank.costPer100Km"> ({{ formatNum(tank.costPer100Km, 2, ' €/100km') }})</span>
                </div>
                <div>
                  {{ formatNum(tank.distanceKm, 1, ' km') }} · Getankt: {{ formatNum(tank.actualLiters, 2, ' L') }} ·
                  CAN-Schätzung: {{ formatNum(tank.estimatedLiters, 2, ' L') }} (Abw. {{ formatNum(tank.estimateErrorPct, 1, '%') }}) ·
                  Gesamtkosten: {{ formatNum(tank.costEur, 2, ' €') }}
                </div>
              </div>
            </div>

            <h4>Letzte Einzelfahrten ({{ (fuel.trips || []).length }})</h4>
            <div v-for="trip in (fuel.trips || []).slice(0, 15)" :key="trip.id" class="gx-row">
              <div class="gx-row__info">
                <div class="gx-row__label">{{ trip.label || formatTimestamp(trip.startedAt) }}</div>
                <div>
                  {{ formatNum(trip.distanceKm, 2, ' km') }} · {{ formatDuration(trip.durationS) }} ·
                  <strong>{{ formatNum(trip.fuelLiters, 2, ' L') }}</strong> (<strong>{{ formatNum(trip.avgLPer100Km, 2, ' L/100km') }}</strong>) ·
                  EV {{ formatNum(trip.evTimePct, 0, '%') }} · Tacho {{ formatNum(trip.clusterAvgLPer100Km ?? trip.clusterAvgFci, 1, ' L/100km') }} ·
                  {{ formatNum(trip.estimatedCostEur, 2, ' €') }} · Quelle: {{ trip.sourceMode }}
                </div>
              </div>
              <button type="button" class="gx-btn gx-btn--tonal" :disabled="!state.data.parked || state.busy"
                @click="confirmAction('delete_trip', 'Fahrt löschen?', 'Diese aufgezeichnete Fahrt endgültig entfernen?', { id: trip.id })">Löschen</button>
            </div>
          </div>

          <!-- TAB 2: ECU & DTC DIAGNOSTICS -->
          <div v-if="state.tab === 'dtc'" class="gx-stack">
            <div class="gx-row gx-row--borderless">
              <span class="gx-row__info gx-row__label">Tempomat-Hauptschalter (Cruise Main) nach Motorstart automatisch aktivieren</span>
              <label class="gx-switch"><input type="checkbox" role="switch" :checked="state.data.config?.autoCruiseMain !== false"
                :disabled="state.busy || !state.data.parked" @change="saveConfigToggle('autoCruiseMain', $event.target.checked)"><span class="gx-switch__track"></span><span class="gx-switch__thumb"></span></label>
            </div>
            <div class="gx-row gx-row--borderless">
              <span class="gx-row__info gx-row__label">Automatisches Steuergeräte-Auslesen beim Start (nur bei Bedarf aktivieren, verzögert Panda-Start kurz)</span>
              <label class="gx-switch"><input type="checkbox" role="switch" :checked="Boolean(state.data.config?.scanOnStart)"
                :disabled="state.busy || !state.data.parked" @change="saveConfigToggle('scanOnStart', $event.target.checked)"><span class="gx-switch__track"></span><span class="gx-switch__thumb"></span></label>
            </div>

            <!-- 12V IBS & HEV THERMAL HEALTH CARDS -->
            <div class="gx-panel gx-stack">
              <strong>12V-Hilfsbatterie &amp; IBS-Diagnose (Kofferraum / LDC-Wandler)</strong>
              <p>
                Status: <strong>{{ battery12v.label || 'Warte auf BAT11…' }}</strong> ·
                Spannung: <strong>{{ formatNum(battery12v.voltageV ?? liveSummary.bat12vVoltageV, 2, ' V') }}</strong> ·
                Ladezustand (SoC): <strong>{{ formatNum(battery12v.socPct ?? liveSummary.bat12vSocPct, 0, '%') }}</strong> ·
                Gesundheit (SoH): <strong>{{ formatNum(battery12v.sohPct ?? liveSummary.bat12vSohPct, 0, '%') }}</strong> ·
                Strom: {{ formatNum(battery12v.currentA ?? liveSummary.bat12vCurrentA, 1, ' A') }} ·
                LDC-Ladung: {{ (battery12v.ldcCharging || battery12v.chargingActive) ? 'Aktiv (HV → 12V)' : 'Ruhe / Entladung' }}
              </p>
              <p v-if="battery12v.advice">{{ battery12v.advice }}</p>
            </div>

            <div class="gx-panel gx-stack">
              <strong>Hybrid-Thermik &amp; Warmlauf-Monitor (1.6 GDi Kappa)</strong>
              <p>
                Phase: <strong>{{ thermal.label || 'Warte auf EMS12…' }}</strong> ·
                Kühlmittel: <strong>{{ formatNum(thermal.coolantTempC ?? liveSummary.coolantTempC, 0, ' °C') }}</strong> ·
                Ansaugluft: {{ formatNum(thermal.intakeAirTempC ?? thermal.intakeTempC ?? liveSummary.intakeTempC, 0, ' °C') }} ·
                Außentemp.: {{ formatNum(thermal.ambientTempC ?? liveSummary.ambientTempC, 1, ' °C') }} ·
                Innenraum: {{ formatNum(thermal.cabinTempC ?? liveSummary.cabinTempC, 1, ' °C') }} ·
                Motor: {{ formatNum(liveSummary.engineRpm, 0, ' U/min') }} ·
                MIL im Tacho: <strong>{{ liveSummary.milLamp ? 'AKTIV (Gelb)' : 'Aus' }}</strong>
              </p>
              <p v-if="thermal.explanation">{{ thermal.explanation }}</p>
            </div>

            <div class="gx-actions">
              <button type="button" class="gx-btn" :disabled="state.busy" @click="act('scan_dtcs')">Steuergeräte jetzt scannen / vormerken</button>
              <select class="gx-field" v-model="state.clearTarget">
                <option value="ALL">Alle Steuergeräte (ALL)</option>
                <option v-for="ecu in state.data.ecuCatalog" :key="ecu.ecuId" :value="ecu.ecuId">{{ ecu.ecuId }} ({{ ecu.txHex }})</option>
              </select>
              <button type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy || !state.data.parked"
                @click="confirmAction('clear_dtcs', 'Fehlercodes wirklich löschen?', 'Ausgewählte DTCs werden im Steuergerät gelöscht.', { target: state.clearTarget })">DTCs löschen</button>
              <button v-if="passiveWarnings.length || warningHistory.length" type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy"
                @click="act('clear_passive_warnings')">CAN-Warnungshistorie zurücksetzen</button>
            </div>

            <GxNotice v-if="diagnostics.pendingAction" tone="info">
              Vorgemerkt für nächstes ELM327-Diagnosefenster (Zündung EIN, Motor aus): {{ diagnostics.pendingAction.type.toUpperCase() }} ({{ diagnostics.pendingAction.target }})
              <button type="button" class="gx-btn gx-btn--tonal" @click="act('cancel_pending_diag')">Abbrechen</button>
            </GxNotice>

            <template v-for="w in passiveWarnings" :key="w.code">
              <GxNotice tone="warning">[Aktive Live-CAN Warnung · {{ w.ecuId }}] {{ w.message }}</GxNotice>
            </template>

            <template v-if="lastScan">
              <div class="gx-panel gx-stack">
                <strong>Diagnose-Zusammenfassung ({{ formatTimestamp(lastScan.scannedAt) }})</strong>
                <p>
                  Steuergeräte online: <strong>{{ lastScan.ecusResponded }}/{{ lastScan.ecusTotal }}</strong> ·
                  Gespeicherte DTCs: <strong>{{ (lastScan.dtcs || []).length }}</strong>
                  (Aktiv: {{ lastScan.analysis?.activeCount || 0 }} · Ausstehend: {{ lastScan.analysis?.pendingCount || 0 }} · Historisch: {{ lastScan.analysis?.historicCount || 0 }}) ·
                  Handlungsempfehlung: <strong>{{ lastScan.analysis?.overallUrgencyLabel || 'Unauffällig' }}</strong>
                </p>
                <p v-if="lastScan.readiness">
                  OBD-II Readiness (0x7E0): MIL {{ lastScan.readiness.milOn ? 'AN' : 'Aus' }} ·
                  Abgas-DTCs: {{ lastScan.readiness.emissionDtcCount }} ·
                  Offene Selbsttests: {{ (lastScan.readiness.incompleteMonitors || []).length ? lastScan.readiness.incompleteMonitors.join(', ') : 'Alle abgeschlossen' }}
                </p>
                <p v-if="lastScan.freezeFrame">
                  OBD Freeze-Frame (Umgebungsdaten beim Auftreten von {{ lastScan.freezeFrame.dtcCode }}):
                  Drehzahl {{ formatNum(lastScan.freezeFrame.rpm, 0, ' U/min') }} ·
                  Geschwindigkeit {{ formatNum(lastScan.freezeFrame.speedKph, 0, ' km/h') }} ·
                  Kühlmittel {{ formatNum(lastScan.freezeFrame.coolantTempC, 0, ' °C') }} ·
                  Motorlast {{ formatNum(lastScan.freezeFrame.engineLoadPct, 1, '%') }}
                </p>
                <p v-if="lastScan.analysis?.milExplanation">{{ lastScan.analysis.milExplanation }}</p>
              </div>

              <div v-for="ins in (lastScan.analysis?.insights || [])" :key="ins.title" class="gx-row">
                <div class="gx-row__info">
                  <div class="gx-row__label">{{ ins.title }}</div>
                  <div>{{ ins.detail }}</div>
                </div>
              </div>

              <h4>Detaillierte Fehlercode-Analyse (DTCs)</h4>
              <p v-if="!(lastScan.dtcs || []).length">Keine gespeicherten Fehlercodes im letzten Scan.</p>
              <div v-for="dtc in (lastScan.dtcs || [])" :key="dtc.ecuId + dtc.code + dtc.source" class="gx-panel gx-stack">
                <div class="gx-row__label">
                  <strong>{{ dtc.code }}</strong> · {{ dtc.ecuId }} ({{ dtc.system }}) ·
                  {{ dtc.active ? '[AKTIV]' : dtc.pending ? '[AUSSTEHEND / SPORADISCH]' : '[GESPEICHERT / HISTORISCH]' }} ·
                  Empfehlung: <strong>{{ dtc.urgencyLabel || 'Prüfen' }}</strong>
                </div>
                <div><strong>{{ dtc.title }}</strong></div>
                <div v-if="dtc.stateExplanation">{{ dtc.stateExplanation }}</div>
                <div><strong>Werkstatt- &amp; Handlungshinweis:</strong> {{ dtc.workshopAdvice || dtc.hint }}</div>
                <div v-if="(dtc.causes || []).length">
                  <strong>Typische Ursachen beim 2016 Ioniq Hybrid:</strong> {{ dtc.causes.join(' · ') }}
                </div>
                <div v-if="dtc.freezeFrame">
                  <strong>Freeze-Frame beim Fehler:</strong>
                  {{ formatNum(dtc.freezeFrame.rpm, 0, ' U/min') }},
                  {{ formatNum(dtc.freezeFrame.speedKph, 0, ' km/h') }},
                  Kühlmittel {{ formatNum(dtc.freezeFrame.coolantTempC, 0, ' °C') }},
                  Last {{ formatNum(dtc.freezeFrame.engineLoadPct, 1, '%') }}
                </div>
                <div v-if="dtc.snapshotHex">
                  <strong>UDS 0x19 0x04 Snapshot-Daten:</strong> <code>{{ dtc.snapshotHex }}</code>
                </div>
              </div>

              <h4>Steuergeräte Übersicht</h4>
              <div v-for="ecu in (lastScan.ecus || [])" :key="ecu.ecuId" class="gx-row">
                <div class="gx-row__info">
                  <div class="gx-row__label">{{ ecu.ecuId }} · {{ ecu.name }} ({{ ecu.txHex }}/{{ ecu.rxHex }})</div>
                  <div>{{ ecu.online ? 'Online' : 'Keine Antwort' }} · DTCs: {{ ecu.dtcCount }} <span v-if="ecu.partNumber">· Teilenr: {{ ecu.partNumber }}</span> <span v-if="ecu.softwareVersion">· SW: {{ ecu.softwareVersion }}</span></div>
                </div>
              </div>
            </template>
            <div v-else class="gx-panel gx-stack">
              <strong>Noch kein aktiver ECU-Scan ausgeführt</strong>
              <p>
                Tipp zur Motorkontrollleuchte (MIL): Wenn die gelbe Motorkontrollleuchte in der Vergangenheit kurz an war,
                bleibt der auslösende Fehlercode (inkl. Freeze-Frame-Umgebungsdaten) im Motor- oder Hybridsteuergerät gespeichert.
                Tippe oben im Stand (Zündung EIN) auf „Steuergeräte jetzt scannen / vormerken“, um alle 14 Steuergeräte auszulesen.
              </p>
            </div>
          </div>

          <!-- TAB 3: 3X BUTTON LEARNER -->
          <div v-if="state.tab === 'buttons'" class="gx-stack">
            <p>Drücke eine Taste im Auto 3x hintereinander. Counters und Prüfsummen werden automatisch herausgefiltert.</p>
            <div class="gx-actions">
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
            <div v-for="b in bindings" :key="b.id" class="gx-row">
              <div class="gx-row__info">
                <div class="gx-row__label">{{ b.name }} — Bus {{ b.bus }} · {{ b.addressHex }} Byte {{ b.byteIndex }} Maske {{ b.bitMaskHex }} ({{ b.mode }})</div>
                <div>Auslösungen: {{ b.triggerCount || 0 }} · Aktion: {{ b.action }}</div>
              </div>
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
              <span class="gx-row__info gx-row__label">Automatische Vor/Nach-Aufzeichnung bei echten CAN-Fehlern (Start-Lampentest wird ausgeblendet)</span>
              <label class="gx-switch"><input type="checkbox" role="switch" :checked="state.data.config?.recorderAutoOnFault"
                :disabled="state.busy || !state.data.parked" @change="saveConfigToggle('recorderAutoOnFault', $event.target.checked)"><span class="gx-switch__track"></span><span class="gx-switch__thumb"></span></label>
            </div>
            <p>Ringpuffer: {{ state.data.recordings?.ringBufferSamples || 0 }} Samples · Vollaufzeichnung: {{ state.data.recordings?.continuousActive ? 'LÄUFT' : 'Inaktiv' }}</p>
            <div class="gx-actions">
              <input class="gx-field" v-model="state.snapshotTitle" maxlength="80" placeholder="Titel für Momentaufnahme">
              <button type="button" class="gx-btn" :disabled="state.busy" @click="triggerSnapshot()">Momentaufnahme speichern</button>
              <button v-if="!state.data.recordings?.continuousActive" type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy" @click="act('start_continuous_recording', { label: state.continuousLabel })">Vollaufzeichnung starten</button>
              <button v-else type="button" class="gx-btn" :disabled="state.busy" @click="act('stop_continuous_recording')">Vollaufzeichnung stoppen</button>
            </div>

            <div v-for="rec in recordingsList" :key="rec.id" class="gx-row">
              <div class="gx-row__info">
                <div class="gx-row__label">[{{ rec.reason }}] {{ rec.title }}</div>
                <div>{{ formatTimestamp(rec.startedAt) }} · Dauer {{ formatDuration(rec.durationS) }} · {{ rec.sampleCount }} Samples · max {{ rec.maxSpeedKph }} km/h / {{ rec.maxRpm }} U/min</div>
              </div>
              <div class="gx-actions">
                <button type="button" class="gx-btn gx-btn--tonal" @click="inspectRecording(rec.id)">Details</button>
                <button type="button" class="gx-btn gx-btn--tonal" @click="downloadRecordingCsv(rec.id)">CSV</button>
                <button type="button" class="gx-btn gx-btn--tonal" :disabled="!state.data.parked"
                  @click="confirmAction('delete_recording', 'Aufzeichnung löschen?', 'Diagnose-Datei dauerhaft löschen?', { id: rec.id })">Löschen</button>
              </div>
            </div>

            <div v-if="state.inspectedRecording" class="gx-panel gx-stack">
              <div class="gx-actions">
                <strong>Aufzeichnung: {{ state.inspectedRecording.title }}</strong>
                <button type="button" class="gx-btn gx-btn--tonal" @click="state.inspectedRecording = null">Schließen</button>
              </div>
              <div v-for="ev in (state.inspectedRecording.events || [])" :key="ev.t + ev.message">
                {{ formatTimestamp(ev.t) }} [{{ ev.kind }}]: {{ ev.message }}
              </div>
              <p>Enthaltene Telemetrie-Zeilen: {{ (state.inspectedRecording.samples || []).length }} (CSV-Export liefert alle Zeitreihenwerte).</p>
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
