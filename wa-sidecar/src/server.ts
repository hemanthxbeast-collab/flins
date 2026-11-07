// Local WebSocket server that flins-core connects to. Loopback only, one client, signed frames.

import type { IncomingMessage } from 'node:http'
import { WebSocket, WebSocketServer } from 'ws'
import { FrameVerifier, ProtocolError, signFrame } from './protocol.js'

const BUFFER_LIMIT = 1000
const HELLO_TIMEOUT_MS = 5000
const LOOPBACK = new Set(['127.0.0.1', '::1', '::ffff:127.0.0.1'])

export const CLOSE_UNAUTHORIZED = 4401
export const CLOSE_BUSY = 4409

export interface ServerOptions {
	port: number
	secret: string
	host?: string
	helloInfo: () => Record<string, unknown>
	onShutdown?: () => void
	/** Any other verified command from core (e.g. send_text). */
	onCommand?: (kind: string, payload: Record<string, unknown>) => void
	log?: (msg: string) => void
}

export class CoreServer {
	private wss: WebSocketServer | null = null
	private client: WebSocket | null = null
	private authed = false
	private buffer: Array<{ kind: string; payload: Record<string, unknown> }> = []
	private readonly log: (msg: string) => void

	constructor(private readonly opts: ServerOptions) {
		this.log = opts.log ?? (() => {})
	}

	get port(): number {
		const addr = this.wss?.address()
		return typeof addr === 'object' && addr ? addr.port : this.opts.port
	}

	get hasClient(): boolean {
		return this.client !== null && this.authed
	}

	start(): Promise<void> {
		return new Promise((resolve, reject) => {
			const wss = new WebSocketServer({ host: this.opts.host ?? '127.0.0.1', port: this.opts.port })
			wss.once('listening', () => resolve())
			wss.once('error', reject)
			wss.on('connection', (ws, req) => this.onConnection(ws, req))
			this.wss = wss
		})
	}

	/** Send to core, or buffer until a client connects (oldest dropped past the limit). */
	emit(kind: string, payload: Record<string, unknown>): void {
		if (this.client && this.authed && this.client.readyState === WebSocket.OPEN) {
			this.client.send(signFrame(this.opts.secret, kind, payload))
			return
		}
		this.buffer.push({ kind, payload })
		if (this.buffer.length > BUFFER_LIMIT) this.buffer.shift()
	}

	async close(): Promise<void> {
		this.client?.close(1001, 'sidecar shutting down')
		await new Promise<void>(resolve => (this.wss ? this.wss.close(() => resolve()) : resolve()))
	}

	private onConnection(ws: WebSocket, req: IncomingMessage): void {
		const remote = req.socket.remoteAddress ?? ''
		if (!LOOPBACK.has(remote)) {
			ws.close(CLOSE_UNAUTHORIZED, 'loopback only')
			return
		}
		if (this.client) {
			ws.close(CLOSE_BUSY, 'another client is connected')
			return
		}
		this.client = ws
		this.authed = false
		const verifier = new FrameVerifier(this.opts.secret)
		// Free the slot right away so a well-behaved client can connect while this one closes.
		const reject = (reason: string): void => {
			if (this.client === ws) {
				this.client = null
				this.authed = false
			}
			ws.close(CLOSE_UNAUTHORIZED, reason)
		}
		const helloTimer = setTimeout(() => reject('no hello'), HELLO_TIMEOUT_MS)

		ws.on('message', data => {
			let body
			try {
				body = verifier.verify(data.toString())
			} catch (err) {
				const reason = err instanceof ProtocolError ? err.message : 'invalid frame'
				this.log(`rejected frame from core: ${reason}`)
				clearTimeout(helloTimer)
				reject(reason)
				return
			}
			if (!this.authed) {
				if (body.kind !== 'hello') {
					clearTimeout(helloTimer)
					reject('expected hello')
					return
				}
				clearTimeout(helloTimer)
				this.authed = true
				ws.send(signFrame(this.opts.secret, 'hello.ok', this.opts.helloInfo()))
				const pending = this.buffer
				this.buffer = []
				for (const e of pending) ws.send(signFrame(this.opts.secret, e.kind, e.payload))
				this.log(`core connected, flushed ${pending.length} buffered events`)
				return
			}
			if (body.kind === 'ping') {
				ws.send(signFrame(this.opts.secret, 'pong', { echo: body.id }))
			} else if (body.kind === 'shutdown') {
				this.opts.onShutdown?.()
			} else if (this.opts.onCommand) {
				this.opts.onCommand(body.kind, body.payload)
			} else {
				this.log(`ignored unknown command kind=${body.kind}`)
			}
		})

		ws.on('close', () => {
			clearTimeout(helloTimer)
			if (this.client === ws) {
				this.client = null
				this.authed = false
			}
		})
		ws.on('error', () => {})
	}
}
