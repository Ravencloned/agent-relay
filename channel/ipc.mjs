import { spawn } from 'node:child_process'
import { existsSync, realpathSync } from 'node:fs'
import path from 'node:path'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const BOOTSTRAP = 'import sys;sys.path.insert(0,sys.argv.pop(1));from gcb.channel_ipc import main;main()'
const MAX_OUTPUT = 300_000

function outsideTarget(candidate, label) {
  const rel = path.relative(realpathSync(process.cwd()), candidate)
  if (rel === '' || (!rel.startsWith('..' + path.sep) && rel !== '..' && !path.isAbsolute(rel))) {
    throw new Error(`${label} must be outside Claude's working directory`)
  }
}

export function configuration(env = process.env) {
  const { GCB_PYTHON: python, GCB_SOURCE_DIR: source, GCB_HOME: home, GCB_SESSION_ID: sessionId } = env
  if (![python, source, home].every(p => typeof p === 'string' && path.isAbsolute(p) && existsSync(p))) {
    throw new Error('GCB_PYTHON, GCB_SOURCE_DIR, and GCB_HOME must be existing absolute paths')
  }
  if (!UUID.test(sessionId || '')) throw new Error('GCB_SESSION_ID must be a UUID')
  const sourceDir = realpathSync(source)
  const queueHome = realpathSync(home)
  outsideTarget(sourceDir, 'Agent Relay source')
  outsideTarget(queueHome, 'Private queue')
  const pollMs = Number(env.GCB_POLL_MS || 1000)
  if (!Number.isInteger(pollMs) || pollMs < 1000 || pollMs > 5000) {
    throw new Error('GCB_POLL_MS must be 1000 to 5000')
  }
  return { python: realpathSync(python), sourceDir, queueHome, sessionId: sessionId.toLowerCase(), pollMs }
}

export async function ipc(config, data) {
  const payload = JSON.stringify({ ...data, session_id: config.sessionId })
  if (Buffer.byteLength(payload) > 250_000) throw new Error('IPC payload too large')
  const args = ['-I', '-c', BOOTSTRAP, config.sourceDir, '--home', config.queueHome]
  const child = spawn(config.python, args, { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true, shell: false })
  let output = Buffer.alloc(0)
  let errorSize = 0
  const ended = new Promise((resolve, reject) => {
    const timer = setTimeout(() => { child.kill(); reject(new Error('IPC timeout')) }, 15_000)
    child.stdout.on('data', chunk => {
      output = Buffer.concat([output, chunk])
      if (output.length > MAX_OUTPUT) { child.kill(); reject(new Error('IPC output limit')) }
    })
    child.stderr.on('data', chunk => {
      errorSize += chunk.length
      if (errorSize > 16_000) { child.kill(); reject(new Error('IPC error output limit')) }
    })
    child.on('error', err => { clearTimeout(timer); reject(new Error(`IPC launch failed: ${err.code || 'unknown'}`)) })
    child.on('close', code => {
      clearTimeout(timer)
      try {
        const message = JSON.parse(output.toString('utf8'))
        if (!message || typeof message.ok !== 'boolean') throw new Error('bad shape')
        if (!message.ok || code !== 0) reject(new Error(message.error || 'IPC command failed'))
        else resolve(message.result)
      } catch (err) { reject(new Error('IPC response is invalid')) }
    })
  })
  child.stdin.end(payload)
  return ended
}
