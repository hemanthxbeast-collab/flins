import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { FrameVerifier, hmacHex, MAX_SKEW_MS, ProtocolError, signFrame } from '../protocol.js'

const SECRET = 'x'.repeat(40)

interface Vectors {
	secret: string
	cases: Array<{ body: string; sig: string }>
}
const vectors = JSON.parse(
	readFileSync(new URL('../../../tests/fixtures/protocol_vectors.json', import.meta.url), 'utf8')
) as Vectors

test('shared vectors match the Python implementation', () => {
	for (const c of vectors.cases) {
		assert.equal(hmacHex(vectors.secret, c.body), c.sig)
	}
})

test('round trip', () => {
	const now = 1_762_500_000_000
	const raw = signFrame(SECRET, 'ping', { a: 1 }, { now })
	const body = new FrameVerifier(SECRET).verify(raw, now)
	assert.equal(body.kind, 'ping')
	assert.deepEqual(body.payload, { a: 1 })
})

test('rejects tampering, wrong secret, staleness, replays and bad versions', () => {
	const now = 1_762_500_000_000
	const raw = signFrame(SECRET, 'ping', {}, { now })

	const tampered = JSON.parse(raw) as { body: string; sig: string }
	tampered.body = tampered.body.replace('ping', 'pong')
	assert.throws(() => new FrameVerifier(SECRET).verify(JSON.stringify(tampered), now), /bad signature/)

	assert.throws(() => new FrameVerifier('y'.repeat(40)).verify(raw, now), /bad signature/)
	assert.throws(() => new FrameVerifier(SECRET).verify(raw, now + MAX_SKEW_MS + 1), /stale/)

	const v = new FrameVerifier(SECRET)
	v.verify(raw, now)
	assert.throws(() => v.verify(raw, now), /replayed/)

	const body = JSON.stringify({ v: 2, kind: 'ping', id: 'a', ts: now, payload: {} })
	const v2 = JSON.stringify({ body, sig: hmacHex(SECRET, body) })
	assert.throws(() => new FrameVerifier(SECRET).verify(v2, now), /version/)

	assert.throws(() => new FrameVerifier(SECRET).verify('not json', now), ProtocolError)
})
