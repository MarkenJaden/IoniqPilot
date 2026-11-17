import assert from 'node:assert/strict'
import test from 'node:test'

import { formatDuration, formatNum, IoniqToolsPage } from '../web/js/ioniq-tools.js'
import { GxState } from '../web/js/state.js'

test('ioniq-tools formatting helpers format values cleanly', () => {
  assert.equal(formatDuration(45), '45s')
  assert.equal(formatDuration(125), '2m 05s')
  assert.equal(formatDuration(3665), '1h 01m')
  assert.equal(formatNum(null), '—')
  assert.equal(formatNum(4.567, 2, ' L'), '4.57 L')
})

test('IoniqToolsPage loads local dashboard and submits action cleanly', async () => {
  const requests = []
  const samplePage = {
    version: 1,
    visible: true,
    parked: true,
    readable: true,
    view: 'view-token-1',
    vehicle: { label: '2016 Hyundai Ioniq Hybrid (AE HEV)' },
    config: { scanOnStart: true, recorderAutoOnFault: true, fuelPriceEurPerLiter: 1.79 },
    ecuCatalog: [{ ecuId: 'ECM', name: 'Motor', txHex: '0x7E0', rxHex: '0x7E8' }],
    diagnostics: { lastScan: null, pendingAction: null, passive: { activeWarnings: [] }, liveSummary: {} },
    buttons: { bindings: [], learnStatus: { state: 'idle', detectedPresses: 0, targetPresses: 3, message: 'Bereit' } },
    buttonActions: [{ id: 'trigger_event_recording', label: 'Aufzeichnung' }],
    fuel: { currentTank: { distanceKm: 120.5, estimatedFuelLiters: 5.1 }, trips: [], tankHistory: [], calibrationFactor: 1.0 },
    recordings: { recordings: [], ringBufferSamples: 10, continuousActive: false },
  }

  const fakeFetcher = async (url, options = {}) => {
    requests.push({ url, method: options.method, body: options.body ? JSON.parse(options.body) : null })
    if (url === './api/ioniq-tools') {
      return { ok: true, status: 200, json: async () => samplePage }
    }
    return { ok: true, status: 200, json: async () => ({ saved: true, queued: true }) }
  }

  const state = IoniqToolsPage.setup().state
  const ctx = {
    mode: 'local',
    unauthorized() {},
    fetcher: fakeFetcher,
    state,
    controller: null,
    timer: null,
    generation: 0,
    alive: true,
    pollIntervalMs: 3000,
    request: IoniqToolsPage.methods.request,
    load: IoniqToolsPage.methods.load,
    act: IoniqToolsPage.methods.act,
  }
  await ctx.load()
  clearTimeout(ctx.timer)

  assert.equal(ctx.state.data.vehicle.label, '2016 Hyundai Ioniq Hybrid (AE HEV)')
  assert.equal(ctx.state.refuelPrice, '1.79')

  // Test automatic cost <-> price calculation helpers
  ctx.state.refuelLiters = '30'
  IoniqToolsPage.methods.onRefuelLitersOrPriceChange.call(ctx)
  assert.equal(ctx.state.refuelCostEur, '53.70')
  ctx.state.refuelCostEur = '54.00'
  IoniqToolsPage.methods.onRefuelCostChange.call(ctx)
  assert.equal(ctx.state.refuelPrice, '1.800')

  await ctx.act('scan_dtcs')
  clearTimeout(ctx.timer)
  ctx.alive = false

  assert.equal(requests[0].url, './api/ioniq-tools')
  assert.equal(requests[1].url, './api/ioniq-tools/action')
  assert.equal(requests[1].body.action, 'scan_dtcs')
  assert.equal(requests[1].body.view, 'view-token-1')
})

test('IoniqToolsPage exposes standalone Tools layout and handles mode transitions', () => {
  assert.equal(IoniqToolsPage.components.GxState, GxState)
  assert.match(IoniqToolsPage.template, /<h2>Ioniq Diagnostics &amp; Fuel<\/h2>/)
  assert.match(IoniqToolsPage.template, /Ioniq Diagnostics &amp; Fuel requires the device\. No operation was attempted\./)
  assert.match(IoniqToolsPage.template, /title="Hyundai Ioniq required"/)
  assert.match(IoniqToolsPage.template, /<GxNotice v-if="state\.error" tone="danger">/)
  assert.match(IoniqToolsPage.template, /12V-Hilfsbatterie &amp; IBS-Diagnose/)
  assert.match(IoniqToolsPage.template, /Hybrid-Thermik &amp; Warmlauf-Monitor/)
  assert.match(IoniqToolsPage.template, /Verbrauchs-Datenquellen Diagnose/)

  let loads = 0
  let aborted = 0
  const ctx = {
    mode: 'sample',
    alive: false,
    generation: 0,
    controller: { abort() { aborted++ } },
    timer: null,
    load() { loads++ },
  }
  IoniqToolsPage.mounted.call(ctx)
  assert.equal(ctx.alive, false)
  assert.equal(loads, 0)

  IoniqToolsPage.watch.mode.call(ctx, 'local')
  assert.equal(ctx.alive, true)
  assert.equal(loads, 1)

  IoniqToolsPage.watch.mode.call(ctx, 'sample')
  assert.equal(ctx.alive, false)
  assert.equal(aborted, 1)
  assert.equal(ctx.generation, 1)
})

test('IoniqToolsPage supports hev_model source label and corrected live summary bindings', () => {
  assert.equal(IoniqToolsPage.methods.sourceLabel('hev_model'), 'HEV C-CAN Modell')
  assert.match(IoniqToolsPage.template, /clusterAvgFcValue/)
  assert.match(IoniqToolsPage.template, /clusterAvgFcUnitLabel/)
  assert.match(IoniqToolsPage.template, /sourceDiag\.instantFuelRateLph/)
  assert.match(IoniqToolsPage.template, /HEV-Modell \/ Kennfeld/)
})
