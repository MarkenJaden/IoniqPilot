import { reactive } from "../vendor/vue/vue.esm-browser.js"
import { GxDialog } from "./dialog.js"
import { GxNotice } from "./notice.js"
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
  components: { GxDialog, GxNotice },
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
    passiveWarnings() { return this.diagnostics.passive?.activeWarnings || [] },
    fuel() { return this.state.data?.fuel || {} },
    currentTank() { return this.fuel.currentTank || {} },
    activeTrip() { return this.fuel.activeTrip || null },
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
      }
      if (this.alive) this.timer = setTimeout(() => this.load(), this.pollIntervalMs)
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
      const odo = this.state.refuelOdo ? parseFloat(String(this.state.refuelOdo).replace(",", ".")) : null
      const res = await this.act("log_refuel", {
        liters,
        fullTank: this.state.refuelFull,
        pricePerLiter: Number.isNaN(price) ? null : price,
        odometerKm: Number.isNaN(odo) ? null : odo,
        notes: this.state.refuelNotes,
      })
      if (res?.saved) {
        this.state.refuelLiters = ""
        this.state.refuelNotes = ""
        this.state.notice = this.state.refuelFull
          ? "Volltanken gespeichert & Tankzyklus abgeschlossen."
          : "Teiltanken verbucht."
      }
    },
  },
  template: `<section v-if="mode === 'local' && (state.data?.visible || state.error)" class="gx-card" aria-label="Hyundai Ioniq 2016 Suite">
    <div class="gx-section__header">
      <i class="bi bi-ev-front" aria-hidden="true"></i>
      <span class="gx-section__title">Ioniq Hybrid Custom Suite (2016 AE)</span>
    </div>
    <div class="gx-panel gx-stack">
      <GxNotice v-if="state.error" tone="danger">{{ state.error }}</GxNotice>
      <GxNotice v-if="state.notice" tone="success">{{ state.notice }}</GxNotice>
      <template v-if="state.data">
        <p>{{ state.data.vehicle?.label }} · Status: {{ state.data.parked ? 'Geparkt' : 'In Fahrt (aktive Änderungen gesperrt)' }}</p>
        <div class="gx-actions">
          <button type="button" :class="state.tab === 'fuel' ? 'gx-btn' : 'gx-btn gx-btn--tonal'" @click="state.tab = 'fuel'">Sprit &amp; Trips</button>
          <button type="button" :class="state.tab === 'dtc' ? 'gx-btn' : 'gx-btn gx-btn--tonal'" @click="state.tab = 'dtc'">Steuergeräte &amp; DTCs</button>
          <button type="button" :class="state.tab === 'buttons' ? 'gx-btn' : 'gx-btn gx-btn--tonal'" @click="state.tab = 'buttons'">3x-Knopf Lernen ({{ bindings.length }})</button>
          <button type="button" :class="state.tab === 'recorder' ? 'gx-btn' : 'gx-btn gx-btn--tonal'" @click="state.tab = 'recorder'">Aufzeichnung ({{ recordingsList.length }})</button>
        </div>

        <GxNotice v-if="fuel.refuelAlert" tone="info">
          Tankfüllstand um {{ fuel.refuelAlert.deltaBars }} Balken gestiegen — jetzt Tankstopp eintragen!
          <button type="button" class="gx-btn gx-btn--tonal" @click="act('dismiss_refuel_alert')">Ausblenden</button>
        </GxNotice>

        <!-- TAB 1: FUEL & TRIPS -->
        <div v-if="state.tab === 'fuel'" class="gx-stack">
          <h4>Aktueller Tankzyklus (Volltanken zu Volltanken)</h4>
          <p>
            Distanz: <strong>{{ formatNum(currentTank.distanceKm, 1, ' km') }}</strong> ·
            Geschätzt: <strong>{{ formatNum(currentTank.estimatedFuelLiters, 2, ' L') }}</strong>
            ({{ formatNum(currentTank.estimatedLPer100Km, 2, ' L/100km') }}) ·
            EV-Anteil: <strong>{{ formatNum(currentTank.evDistanceKm, 1, ' km') }}</strong> ·
            Verbrenner: <strong>{{ formatNum(currentTank.iceDistanceKm, 1, ' km') }}</strong> ·
            Fahrten: <strong>{{ currentTank.tripCount || 0 }}</strong> ·
            Kalibrierung: <strong>{{ formatNum(fuel.calibrationFactor, 3, 'x') }}</strong>
          </p>
          <p v-if="fuel.lifetimeTankAvgLPer100Km">
            Realer Gesamtdurchschnitt (Volltanken): <strong>{{ formatNum(fuel.lifetimeTankAvgLPer100Km, 2, ' L/100km') }}</strong>
            <span v-if="liveSummary.fuelDispPct !== null && liveSummary.fuelDispPct !== undefined"> · Tankuhr: {{ formatNum(liveSummary.fuelDispPct, 0, '%') }} ({{ liveSummary.dteKm ?? '—' }} km Rest)</span>
          </p>

          <div v-if="activeTrip" class="gx-panel gx-stack">
            <strong>Laufende Fahrt (Motorstart → Motorstopp)</strong>
            <p>
              Dauer: {{ formatDuration(activeTrip.durationS) }} ·
              Distanz: {{ formatNum(activeTrip.distanceKm, 2, ' km') }} ·
              Verbrauch: {{ formatNum(activeTrip.fuelLiters, 2, ' L') }} ({{ formatNum(activeTrip.avgLPer100Km, 2, ' L/100km') }}) ·
              EV: {{ formatNum(activeTrip.evTimePct, 0, '%') }} ·
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

          <h4>Tankstopp / Volltanken erfassen</h4>
          <div class="gx-actions">
            <label>Getankte Liter<input class="gx-field" type="number" step="0.01" min="0.5" max="90" v-model="state.refuelLiters" placeholder="z.B. 34.85"></label>
            <label>Preis €/L<input class="gx-field" type="number" step="0.001" v-model="state.refuelPrice" placeholder="1.75"></label>
            <label>km-Stand (optional)<input class="gx-field" type="number" step="0.1" v-model="state.refuelOdo"></label>
            <label>Notiz<input class="gx-field" v-model="state.refuelNotes" maxlength="120" placeholder="Tankstelle / E5"></label>
          </div>
          <div class="gx-row gx-row--borderless">
            <span class="gx-row__info gx-row__label">Vollgetankt (Tankzyklus abschließen &amp; Verbrauch kalibrieren)</span>
            <label class="gx-switch"><input type="checkbox" role="switch" v-model="state.refuelFull"><span class="gx-switch__track"></span><span class="gx-switch__thumb"></span></label>
          </div>
          <div class="gx-actions">
            <button type="button" class="gx-btn" :disabled="state.busy || !state.refuelLiters" @click="submitRefuel()">Tanken speichern</button>
            <button type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy" @click="downloadTripsCsv()">Trips CSV exportieren</button>
          </div>

          <h4>Letzte Fahrten ({{ (fuel.trips || []).length }})</h4>
          <div v-for="trip in (fuel.trips || []).slice(0, 15)" :key="trip.id" class="gx-row">
            <div class="gx-row__info">
              <div class="gx-row__label">{{ trip.label || formatTimestamp(trip.startedAt) }}</div>
              <div>
                {{ formatNum(trip.distanceKm, 2, ' km') }} · {{ formatDuration(trip.durationS) }} ·
                {{ formatNum(trip.fuelLiters, 2, ' L') }} ({{ formatNum(trip.avgLPer100Km, 2, ' L/100km') }}) ·
                EV {{ formatNum(trip.evTimePct, 0, '%') }} · Tacho {{ formatNum(trip.clusterAvgFci, 1) }} · {{ formatNum(trip.estimatedCostEur, 2, ' €') }}
              </div>
            </div>
            <button type="button" class="gx-btn gx-btn--tonal" :disabled="!state.data.parked || state.busy"
              @click="confirmAction('delete_trip', 'Fahrt löschen?', 'Diese aufgezeichnete Fahrt endgültig entfernen?', { id: trip.id })">Löschen</button>
          </div>

          <h4>Volltanken-zu-Volltanken Historie ({{ (fuel.tankHistory || []).length }})</h4>
          <div v-for="tank in (fuel.tankHistory || []).slice(0, 10)" :key="tank.id" class="gx-row">
            <div class="gx-row__info">
              <div class="gx-row__label">{{ formatTimestamp(tank.endedAt) }} — Real: {{ formatNum(tank.actualLPer100Km, 2, ' L/100km') }}</div>
              <div>
                {{ formatNum(tank.distanceKm, 1, ' km') }} · Getankt: {{ formatNum(tank.actualLiters, 2, ' L') }} ·
                Geschätzt: {{ formatNum(tank.estimatedLiters, 2, ' L') }} ({{ formatNum(tank.estimateErrorPct, 1, '%') }}) · {{ formatNum(tank.costEur, 2, ' €') }}
              </div>
            </div>
          </div>
        </div>

        <!-- TAB 2: ECU & DTC DIAGNOSTICS -->
        <div v-if="state.tab === 'dtc'" class="gx-stack">
          <div class="gx-row gx-row--borderless">
            <span class="gx-row__info gx-row__label">Automatisches Steuergeräte-Auslesen beim Fahrzeugstart (ELM327 Fenster)</span>
            <label class="gx-switch"><input type="checkbox" role="switch" :checked="state.data.config?.scanOnStart"
              :disabled="state.busy || !state.data.parked" @change="saveConfigToggle('scanOnStart', $event.target.checked)"><span class="gx-switch__track"></span><span class="gx-switch__thumb"></span></label>
          </div>
          <p>
            Live CAN: Motor {{ formatNum(liveSummary.engineRpm, 0, ' U/min') }} ·
            Kühlmittel {{ formatNum(liveSummary.coolantTempC, 0, ' °C') }} ·
            12V {{ formatNum(liveSummary.bat12vVoltageV, 2, ' V') }} (SoC {{ formatNum(liveSummary.bat12vSocPct, 0, '%') }} / SoH {{ formatNum(liveSummary.bat12vSohPct, 0, '%') }}) ·
            MIL {{ liveSummary.milLamp ? 'AN' : 'Aus' }}
          </p>
          <div class="gx-actions">
            <button type="button" class="gx-btn" :disabled="state.busy" @click="act('scan_dtcs')">Steuergeräte jetzt scannen / vormerken</button>
            <select class="gx-field" v-model="state.clearTarget">
              <option value="ALL">Alle Steuergeräte (ALL)</option>
              <option v-for="ecu in state.data.ecuCatalog" :key="ecu.ecuId" :value="ecu.ecuId">{{ ecu.ecuId }} ({{ ecu.txHex }})</option>
            </select>
            <button type="button" class="gx-btn gx-btn--tonal" :disabled="state.busy || !state.data.parked"
              @click="confirmAction('clear_dtcs', 'Fehlercodes wirklich löschen?', 'Ausgewählte DTCs werden im ELM327-Diagnosefenster gelöscht.', { target: state.clearTarget })">DTCs löschen</button>
          </div>

          <GxNotice v-if="diagnostics.pendingAction" tone="info">
            Vorgemerkt für Diagnosefenster beim nächsten Start: {{ diagnostics.pendingAction.type.toUpperCase() }} ({{ diagnostics.pendingAction.target }})
            <button type="button" class="gx-btn gx-btn--tonal" @click="act('cancel_pending_diag')">Abbrechen</button>
          </GxNotice>

          <template v-for="w in passiveWarnings" :key="w.code">
            <GxNotice tone="warning">[Live-CAN {{ w.ecuId }}] {{ w.message }}</GxNotice>
          </template>

          <template v-if="lastScan">
            <p>Letzter Scan: {{ formatTimestamp(lastScan.scannedAt) }} · Steuergeräte geantwortet: {{ lastScan.ecusResponded }}/{{ lastScan.ecusTotal }} · DTCs: {{ (lastScan.dtcs || []).length }}</p>
            <div v-for="ins in (lastScan.analysis?.insights || [])" :key="ins.title" class="gx-row">
              <div class="gx-row__info">
                <div class="gx-row__label">{{ ins.title }}</div>
                <div>{{ ins.detail }}</div>
              </div>
            </div>
            <h4>Gefundene Fehlercodes</h4>
            <p v-if="!(lastScan.dtcs || []).length">Keine gespeicherten Fehlercodes im letzten Scan.</p>
            <div v-for="dtc in (lastScan.dtcs || [])" :key="dtc.ecuId + dtc.code + dtc.source" class="gx-row">
              <div class="gx-row__info">
                <div class="gx-row__label">{{ dtc.code }} · {{ dtc.ecuId }} ({{ dtc.system }}) {{ dtc.active ? '[AKTIV]' : dtc.pending ? '[AUSSTEHEND]' : '[HISTORISCH]' }}</div>
                <div>{{ dtc.title }} — {{ dtc.hint }}</div>
              </div>
            </div>
            <h4>Steuergeräte Übersicht</h4>
            <div v-for="ecu in (lastScan.ecus || [])" :key="ecu.ecuId" class="gx-row">
              <div class="gx-row__info">
                <div class="gx-row__label">{{ ecu.ecuId }} · {{ ecu.name }} ({{ ecu.txHex }}/{{ ecu.rxHex }})</div>
                <div>{{ ecu.online ? 'Online' : 'Keine Antwort' }} · DTCs: {{ ecu.dtcCount }} <span v-if="ecu.partNumber">· Teilenr: {{ ecu.partNumber }}</span></div>
              </div>
            </div>
          </template>
          <p v-else>Noch kein aktiver ECU-Scan ausgeführt. Oben auf „Steuergeräte jetzt scannen / vormerken“ tippen.</p>
        </div>

        <!-- TAB 3: 3X BUTTON LEARNER -->
        <div v-if="state.tab === 'buttons'" class="gx-stack">
          <p>Drücke eine Taste im Auto 3x hintereinander. Counters und Prüfsummen werden automatisch herausgefiltert.</p>
          <div class="gx-actions">
            <label>Name der Taste<input class="gx-field" v-model="state.learnLabel" maxlength="48" placeholder="z.B. EV-Knopf oder Lenkrad-Stern"></label>
            <label>Aktion zuweisen
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
            <span class="gx-row__info gx-row__label">Automatische Vor/Nach-Aufzeichnung bei CAN-Fehlern</span>
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
      </template>

      <GxDialog v-if="state.pendingConfirm" labelledby="gx-ioniq-confirm" @close="cancelConfirm()">
        <h3 id="gx-ioniq-confirm">{{ state.pendingConfirm.title }}</h3>
        <p>{{ state.pendingConfirm.message }}</p>
        <div class="gx-actions">
          <button type="button" class="gx-btn gx-btn--tonal" @click="cancelConfirm()">Abbrechen</button>
          <button type="button" class="gx-btn" @click="runConfirmed()">Bestätigen</button>
        </div>
      </GxDialog>
    </div>
  </section>`,
}
