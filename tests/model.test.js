const test = require("node:test")
const assert = require("node:assert")
const Model = require("../Model.js")

test("parseQrMatrix accepts a square 0/1 matrix", () => {
  const out = Model.parseQrMatrix("101\n010\n111\n")
  assert.equal(out.size, 3)
  assert.deepEqual(out.rows, ["101", "010", "111"])
})

test("parseQrMatrix rejects ragged matrices", () => {
  assert.equal(Model.parseQrMatrix("10\n1").size, 0)
})

test("parseQrMatrix rejects non-binary content", () => {
  assert.equal(Model.parseQrMatrix("1a\n01").size, 0)
})

test("parseQrMatrix rejects empty input", () => {
  assert.equal(Model.parseQrMatrix("").size, 0)
  assert.equal(Model.parseQrMatrix(null).size, 0)
})

test("setupUrl embeds an encoded token", () => {
  assert.equal(
    Model.setupUrl("192.168.1.8", 8737, "a+b"),
    "http://192.168.1.8:8737/setup?token=a%2Bb")
})

test("clipUrl builds the clip endpoint", () => {
  assert.equal(Model.clipUrl("10.0.0.2", 8737), "http://10.0.0.2:8737/clip")
})

test("statusLine reports a stopped bridge before anything else", () => {
  assert.equal(Model.statusLine({ host: "h", port: 1 }, false), "Bridge not running")
})

test("statusLine shows the address when running", () => {
  assert.equal(Model.statusLine({ host: "192.168.1.8", port: 8737 }, true), "http://192.168.1.8:8737")
})

test("statusLine shows starting while running without status", () => {
  assert.equal(Model.statusLine(null, true), "Bridge starting…")
})

test("pairingLine reports the unpaired state", () => {
  assert.equal(Model.pairingLine(null), "Not paired yet")
  assert.equal(Model.pairingLine({}), "Not paired yet")
  assert.equal(Model.pairingLine({ paired: false }), "Not paired yet")
})

test("pairingLine reports pairing without leaking clip contents", () => {
  const status = {
    paired: true,
    paired_at: "2026-08-29T19:54:00",
    last_event: { direction: "phone-to-desktop", preview: '"hunter2"', at: "19:54" }
  }
  assert.equal(Model.pairingLine(status), "Paired")
})
