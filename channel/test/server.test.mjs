import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, readFileSync, rmSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { configuration } from '../ipc.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const server = path.resolve(here, '..', 'server.mjs')
const source = path.join(here, 'fixture')
const sessionId = '550e8400-e29b-41d4-a716-446655440000'
const requestId = '550e8400-e29b-41d4-a716-446655440001'

test('channel offers a reply tool and correlates a notification', async () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'agent-relay-channel-'))
  const cwd = path.join(root, 'worktree')
  const home = path.join(root, 'queue')
  mkdirSync(cwd)
  mkdirSync(home)
  const python = execFileSync(process.platform === 'win32' ? 'python' : 'python3',
    ['-c', 'import sys;print(sys.executable)'], { encoding: 'utf8' }).trim()
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [server],
    cwd,
    env: { ...process.env, GCB_PYTHON: python, GCB_SOURCE_DIR: source,
      GCB_HOME: home, GCB_SESSION_ID: sessionId, GCB_POLL_MS: '1000' },
    stderr: 'pipe',
  })
  const client = new Client({ name: 'test-client', version: '0.1.0' }, { capabilities: {} })
  let resolveEvent
  const event = new Promise(resolve => { resolveEvent = resolve })
  client.fallbackNotificationHandler = async notification => {
    if (notification.method === 'notifications/claude/channel') resolveEvent(notification)
  }
  try {
    await client.connect(transport)
    const tools = await client.listTools()
    assert.equal(tools.tools.length, 1)
    assert.equal(tools.tools[0].name, 'reply')
    const inbound = await Promise.race([event, new Promise((_, reject) => setTimeout(() => reject(new Error('event timeout')), 8000))])
    assert.equal(inbound.params.meta.request_id, requestId)
    assert.equal(inbound.params.content, 'fixture message')
    await assert.rejects(client.callTool({ name: 'reply', arguments: {
      request_id: '550e8400-e29b-41d4-a716-446655440002', text: 'spoofed' } }))
    const response = await client.callTool({ name: 'reply', arguments: { request_id: requestId, text: 'fixture reply' } })
    assert.equal(JSON.parse(response.content[0].text).state, 'completed')
    let state
    for (let attempt = 0; attempt < 20; attempt++) {
      state = JSON.parse(readFileSync(path.join(home, 'state.json'), 'utf8'))
      if (state.emitted) break
      await new Promise(resolve => setTimeout(resolve, 100))
    }
    assert.equal(state.emitted, requestId)
    assert.equal(state.reply_id, requestId)
    assert.equal(state.reply, 'fixture reply')
  } finally {
    await client.close()
    rmSync(root, { recursive: true, force: true })
  }
})

test('configuration rejects target-local code and an invalid session', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'agent-relay-config-'))
  try {
    assert.throws(() => configuration({ GCB_PYTHON: process.execPath,
      GCB_SOURCE_DIR: process.cwd(), GCB_HOME: root, GCB_SESSION_ID: sessionId }),
    /outside Claude/)
    assert.throws(() => configuration({ GCB_PYTHON: process.execPath,
      GCB_SOURCE_DIR: source, GCB_HOME: root, GCB_SESSION_ID: 'not-a-session' }),
    /UUID/)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})
