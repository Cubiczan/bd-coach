/* BD Coach live call surface.
 *
 * Joins an Agora voice channel and streams both sides of the audio to the
 * self-hosted coach service, which transcribes locally and pushes back nudges.
 *
 * The Agora SDK is loaded from a script tag in index.html.
 */

const CHUNK_SECONDS = 4

const ui = {
  callId: document.querySelector('#call-id'),
  role: document.querySelector('#role'),
  secret: document.querySelector('#join-secret'),
  join: document.querySelector('#join'),
  leave: document.querySelector('#leave'),
  status: document.querySelector('#status'),
  nudges: document.querySelector('#nudges'),
  transcript: document.querySelector('#transcript'),
  metrics: document.querySelector('#metrics')
}

const session = {
  client: null,
  micTrack: null,
  socket: null,
  recorders: [],
  startedAt: 0
}

const urlToken = new URLSearchParams(location.search).get('token')
if (urlToken && ui.secret && !ui.secret.value) ui.secret.value = urlToken

function setStatus(text, tone = 'idle') {
  ui.status.textContent = text
  ui.status.dataset.tone = tone
}

function joinSecret() {
  return (ui.secret && ui.secret.value ? ui.secret.value : '').trim()
}

/**
 * MediaRecorder emits a decodable container only in its first blob; later
 * blobs from the same recorder are headerless fragments that Whisper cannot
 * open on their own. So each window gets its own short-lived recorder, which
 * yields a complete, independently decodable file every CHUNK_SECONDS.
 */
function startChunkedCapture(mediaStreamTrack, speaker) {
  if (!mediaStreamTrack) return () => Promise.resolve()

  const stream = new MediaStream([mediaStreamTrack])
  let stopped = false
  let timer = null
  let activeRecorder = null
  let stopResolve = null

  const recordOnce = () => {
    if (stopped) return
    const recorder = new MediaRecorder(stream, { mimeType: 'audio/webm' })
    const parts = []
    const startedAt = (Date.now() - session.startedAt) / 1000
    activeRecorder = recorder

    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) parts.push(event.data)
    }

    recorder.onstop = async () => {
      if (activeRecorder === recorder) activeRecorder = null
      if (stopResolve) {
        const done = stopResolve
        stopResolve = null
        done()
      }
      // After End (or a failed join teardown) do not upload a late chunk.
      if (stopped || parts.length === 0) return
      const blob = new Blob(parts, { type: 'audio/webm' })
      const buffer = await blob.arrayBuffer()
      send({
        type: 'audio',
        speaker,
        at: startedAt,
        duration: (Date.now() - session.startedAt) / 1000 - startedAt,
        data: toBase64(buffer)
      })
    }

    recorder.start()
    timer = setTimeout(() => {
      if (recorder.state !== 'inactive') recorder.stop()
      recordOnce()
    }, CHUNK_SECONDS * 1000)
  }

  recordOnce()

  return () =>
    new Promise((resolve) => {
      stopped = true
      if (timer) clearTimeout(timer)
      timer = null
      if (activeRecorder && activeRecorder.state !== 'inactive') {
        stopResolve = resolve
        activeRecorder.stop()
      } else {
        resolve()
      }
    })
}

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer)
  let binary = ''
  // Chunked to stay under the argument limit on long buffers.
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000))
  }
  return btoa(binary)
}

function send(message) {
  if (session.socket && session.socket.readyState === WebSocket.OPEN) {
    session.socket.send(JSON.stringify(message))
  }
}

function showNudge(payload) {
  const card = document.createElement('article')
  card.className = 'nudge'
  card.innerHTML = `
    <p class="nudge-text"></p>
    <p class="nudge-evidence"></p>
  `
  card.querySelector('.nudge-text').textContent = payload.text
  card.querySelector('.nudge-evidence').textContent =
    payload.evidence + (payload.model_used ? '' : ' · model unavailable, showing raw cue')
  ui.nudges.prepend(card)

  // Nudges are for right now; anything older is noise on a live call.
  while (ui.nudges.children.length > 4) ui.nudges.lastChild.remove()
}

function showTranscript(payload) {
  const line = document.createElement('p')
  line.className = `line line-${payload.speaker}`
  line.textContent = `${payload.speaker}: ${payload.text}`
  ui.transcript.prepend(line)
  while (ui.transcript.children.length > 40) ui.transcript.lastChild.remove()
}

function showSummary(payload) {
  ui.metrics.textContent =
    `${Math.round(payload.duration_seconds / 60)} min · ` +
    `talk ratio ${Math.round(payload.talk_ratio * 100)}% · ` +
    `${payload.seller_questions} questions asked · ` +
    `longest monologue ${Math.round(payload.longest_monologue_seconds)}s`
}

async function teardown({ sendEnd = false } = {}) {
  const stops = session.recorders.splice(0)
  await Promise.all(stops.map((stop) => Promise.resolve(stop())))

  if (sendEnd) send({ type: 'end' })

  if (session.micTrack) {
    session.micTrack.stop()
    session.micTrack.close()
    session.micTrack = null
  }
  if (session.client) {
    try {
      await session.client.leave()
    } catch (_error) {
      // Never joined, or Agora already tore the client down.
    }
    session.client.removeAllListeners()
    session.client = null
  }

  const socket = session.socket
  session.socket = null
  if (socket) {
    if (sendEnd) {
      setTimeout(() => socket.close(), 1000)
    } else {
      socket.close()
    }
  }
}

async function join() {
  const callId = ui.callId.value.trim()
  const role = ui.role && ui.role.value === 'prospect' ? 'prospect' : 'seller'
  const secret = joinSecret()
  if (!callId) {
    setStatus('Enter a call id first', 'error')
    return
  }

  ui.join.disabled = true
  setStatus('Requesting credentials…')

  try {
    const headers = { 'Content-Type': 'application/json' }
    if (secret) headers.Authorization = `Bearer ${secret}`
    const response = await fetch('/token', {
      method: 'POST',
      headers,
      body: JSON.stringify({ call_id: callId, role })
    })
    const credentials = await response.json()
    if (!response.ok) throw new Error(credentials.detail || 'token request failed')

    session.startedAt = Date.now()
    const tokenQs = secret ? `?token=${encodeURIComponent(secret)}` : ''
    const socket = new WebSocket(
      `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/coach${tokenQs}`
    )
    session.socket = socket
    socket.onmessage = (event) => {
      const payload = JSON.parse(event.data)
      if (payload.type === 'nudge') showNudge(payload)
      else if (payload.type === 'transcript') showTranscript(payload)
      else if (payload.type === 'summary') showSummary(payload)
    }
    socket.onclose = () => {
      if (session.socket === socket) setStatus('Coach disconnected', 'error')
    }

    const client = AgoraRTC.createClient({ mode: 'rtc', codec: 'vp8' })
    session.client = client
    const remoteSpeaker = role === 'seller' ? 'prospect' : 'seller'

    client.on('user-published', async (user, mediaType) => {
      if (mediaType !== 'audio') return
      await client.subscribe(user, mediaType)
      user.audioTrack.play()
      // Coach the other side of the conversation too — talk ratio is meaningless
      // with only one speaker measured.
      session.recorders.push(
        startChunkedCapture(user.audioTrack.getMediaStreamTrack(), remoteSpeaker)
      )
    })

    await client.join(credentials.app_id, credentials.channel, credentials.token, credentials.uid)

    session.micTrack = await AgoraRTC.createMicrophoneAudioTrack()
    await client.publish([session.micTrack])
    session.recorders.push(startChunkedCapture(session.micTrack.getMediaStreamTrack(), role))

    ui.leave.disabled = false
    setStatus(`On call · ${credentials.channel}`, 'live')
  } catch (error) {
    try {
      await teardown({ sendEnd: false })
    } catch (_teardownError) {
      // Restore controls even if cleanup throws.
    }
    ui.leave.disabled = true
    ui.join.disabled = false
    setStatus(error.message, 'error')
  }
}

async function leave() {
  ui.leave.disabled = true
  try {
    await teardown({ sendEnd: true })
    setStatus('Call ended', 'idle')
  } catch (error) {
    setStatus(error.message, 'error')
  } finally {
    ui.join.disabled = false
  }
}

ui.join.addEventListener('click', () => join().catch((e) => setStatus(e.message, 'error')))
ui.leave.addEventListener('click', () => leave().catch((e) => setStatus(e.message, 'error')))

fetch('/healthz')
  .then((r) => r.json())
  .then((health) => {
    if (!health.call_surface) {
      setStatus('Agora credentials not configured — call surface disabled', 'error')
      ui.join.disabled = true
    } else if (health.join_guard === false) {
      setStatus('COACH_JOIN_SECRET is unset — call surface locked', 'error')
      ui.join.disabled = true
    }
  })
  .catch(() => setStatus('Coach service unreachable', 'error'))
