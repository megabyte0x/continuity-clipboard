// Pure helpers for the Continuity Clipboard panel. No QML imports, so the
// same file loads under plain node for tests (wifiqr Model.js pattern).

// Parses make-qr.sh output: a square matrix of 0/1 rows. A malformed matrix
// returns empty rather than rendering a code that cannot scan.
function parseQrMatrix(raw) {
  var lines = String(raw || "").trim().split(/\r?\n/).filter(function(line) { return line !== "" })
  if (lines.length === 0) return { rows: [], size: 0 }

  var size = lines[0].length
  if (size !== lines.length) return { rows: [], size: 0 }

  for (var i = 0; i < lines.length; i++) {
    if (lines[i].length !== size || !/^[01]+$/.test(lines[i])) return { rows: [], size: 0 }
  }

  return { rows: lines, size: size }
}

function baseUrl(host, port) {
  return "http://" + String(host || "") + ":" + String(port)
}

// The camera-scan pairing URL. The token rides the query string because the
// iPhone camera can only open plain links.
function setupUrl(host, port, token) {
  return baseUrl(host, port) + "/setup?token=" + encodeURIComponent(String(token || ""))
}

function clipUrl(host, port) {
  return baseUrl(host, port) + "/clip"
}

// One-line bridge state for the panel header.
function statusLine(status, daemonRunning) {
  if (!daemonRunning) return "Bridge not running"
  var host = status && status.pair_host ? status.pair_host : (status && status.host)
  if (host && status.port) return baseUrl(host, status.port)
  return "Bridge starting…"
}

// One-line description of the last synced clip, or a friendly empty state.
function eventLine(status) {
  var event = status && status.last_event ? status.last_event : null
  if (!event || !event.preview) return "No clips synced yet"
  var arrow = event.direction === "phone-to-desktop" ? "iPhone → desktop" : "desktop → iPhone"
  var at = event.at ? "  ·  " + event.at : ""
  return arrow + ": " + event.preview + at
}

if (typeof module !== "undefined") {
  module.exports = {
    parseQrMatrix: parseQrMatrix,
    baseUrl: baseUrl,
    setupUrl: setupUrl,
    clipUrl: clipUrl,
    statusLine: statusLine,
    eventLine: eventLine
  }
}
