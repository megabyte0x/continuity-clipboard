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

test("eventLine has a friendly empty state", () => {
  assert.equal(Model.eventLine(null), "No clips synced yet")
  assert.equal(Model.eventLine({}), "No clips synced yet")
})

test("eventLine formats direction, preview, and time", () => {
  const line = Model.eventLine({ last_event: { direction: "phone-to-desktop", preview: '"Hi"', at: "20:31" } })
  assert.equal(line, 'iPhone → desktop: "Hi"  ·  20:31')
  const out = Model.eventLine({ last_event: { direction: "desktop-to-phone", preview: '"Yo"', at: "20:32" } })
  assert.equal(out, 'desktop → iPhone: "Yo"  ·  20:32')
})
