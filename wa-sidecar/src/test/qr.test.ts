import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { qrMatrix, writePairState } from '../qr.js'

test('qrMatrix makes a square grid with finder patterns', () => {
	const m = qrMatrix('2@abc,def,ghi')
	assert.ok(m.length >= 21)
	assert.ok(m.every(row => row.length === m.length))
	// Top-left finder pattern: a dark 7x7 border.
	for (let i = 0; i < 7; i++) {
		assert.equal(m[0]?.[i], 1)
		assert.equal(m[i]?.[0], 1)
	}
})

test('writePairState writes valid JSON', () => {
	const f = join(mkdtempSync(join(tmpdir(), 'qr-')), 'pair.json')
	writePairState(f, { state: 'failed', reason: 'x' })
	const data = JSON.parse(readFileSync(f, 'utf8')) as { state: string; ts: number }
	assert.equal(data.state, 'failed')
	assert.ok(data.ts > 0)
})
