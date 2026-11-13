import assert from 'node:assert/strict'
import test from 'node:test'

import { formatDuration, formatNum, IoniqToolsPage } from '../web/js/ioniq-tools.js'

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

  await ctx.act('scan_dtcs')
  clearTimeout(ctx.timer)
  ctx.alive = false

  assert.equal(requests[0].url, './api/ioniq-tools')
  assert.equal(requests[1].url, './api/ioniq-tools/action')
  assert.equal(requests[1].body.action, 'scan_dtcs')
  assert.equal(requests[1].body.view, 'view-token-1')
})
