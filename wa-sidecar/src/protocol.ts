// Wire protocol v1 between flins-core and this sidecar. Mirror of src/flins/adapters/protocol.py.
// Frame: {"body": "<json string>", "sig": "<hex HMAC-SHA256(secret, body)>"}
// Body:  {"v": 1, "kind": str, "id": str, "ts": epoch_ms, "payload": {...}}

import { createHmac, randomUUID, timingSafeEqual } from 'node:crypto'

export const PROTOCOL_VERSION = 1
export const MAX_SKEW_MS = 120_000
export const SEEN_IDS = 2000
export const MIN_SECRET_LENGTH = 32

export interface Body {
	v: number
	kind: string
	id: string
	ts: number
	payload: Record<string, unknown>
}

export class ProtocolError extends Error {}

export function hmacHex(secret: string, body: string): string {
	return createHmac('sha256', secret).update(body, 'utf8').digest('hex')
}

export function signFrame(
	secret: string,
	kind: string,
	payload: Record<string, unknown>,
	opts: { now?: number; id?: string } = {}
): string {
	const body: Body = {
		v: PROTOCOL_VERSION,
		kind,
		id: opts.id ?? randomUUID(),
		ts: opts.now ?? Date.now(),
		payload
	}
	const bodyStr = JSON.stringify(body)
	return JSON.stringify({ body: bodyStr, sig: hmacHex(secret, bodyStr) })
}

/** Checks signature, version, clock skew and replays. One instance per receiving side. */
export class FrameVerifier {
	private readonly seen: string[] = []
	private readonly seenSet = new Set<string>()

	constructor(private readonly secret: string) {}

	verify(raw: string, now: number = Date.now()): Body {
		let frame: unknown
		try {
			frame = JSON.parse(raw)
		} catch {
			throw new ProtocolError('frame is not JSON')
		}
		if (!isRecord(frame) || typeof frame.body !== 'string' || typeof frame.sig !== 'string') {
			throw new ProtocolError('frame must be {body: string, sig: string}')
		}
		const expected = Buffer.from(hmacHex(this.secret, frame.body), 'hex')
		const given = Buffer.from(frame.sig, 'hex')
		if (given.length !== expected.length || !timingSafeEqual(given, expected)) {
			throw new ProtocolError('bad signature')
		}
		let body: unknown
		try {
			body = JSON.parse(frame.body)
		} catch {
			throw new ProtocolError('body is not JSON')
		}
		if (
			!isRecord(body) ||
			typeof body.kind !== 'string' ||
			typeof body.id !== 'string' ||
			typeof body.ts !== 'number' ||
			!isRecord(body.payload)
		) {
			throw new ProtocolError('malformed body')
		}
		if (body.v !== PROTOCOL_VERSION) {
			throw new ProtocolError(`unsupported protocol version ${String(body.v)}`)
		}
		if (Math.abs(now - body.ts) > MAX_SKEW_MS) {
			throw new ProtocolError('stale or future frame')
		}
		if (this.seenSet.has(body.id)) {
			throw new ProtocolError('replayed frame id')
		}
		this.seen.push(body.id)
		this.seenSet.add(body.id)
		if (this.seen.length > SEEN_IDS) {
			this.seenSet.delete(this.seen.shift()!)
		}
		return body as unknown as Body
	}
}

function isRecord(x: unknown): x is Record<string, unknown> {
	return typeof x === 'object' && x !== null && !Array.isArray(x)
}
