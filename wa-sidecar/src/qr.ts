// Pairing QR for the dashboard: the QR as a grid of dark/light squares in a small JSON file.
// FLINS draws it itself, so no image library and no outside scripts are needed. The encoder is
// the one bundled with qrcode-terminal (MIT, Kazuhiko Arase), already a dependency.

import { renameSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'

interface QREncoder {
	addData(data: string): void
	make(): void
	getModuleCount(): number
	isDark(row: number, col: number): boolean
}

const require = createRequire(import.meta.url)
const QRCode = require('qrcode-terminal/vendor/QRCode') as new (
	typeNumber: number,
	level: number
) => QREncoder
const QRErrorCorrectLevel = require('qrcode-terminal/vendor/QRCode/QRErrorCorrectLevel') as {
	L: number
	M: number
}

/** Rows of 0/1 (1 = dark). */
export function qrMatrix(text: string): number[][] {
	const qr = new QRCode(-1, QRErrorCorrectLevel.L)
	qr.addData(text)
	qr.make()
	const n = qr.getModuleCount()
	const rows: number[][] = []
	for (let r = 0; r < n; r++) {
		const row: number[] = []
		for (let c = 0; c < n; c++) row.push(qr.isDark(r, c) ? 1 : 0)
		rows.push(row)
	}
	return rows
}

export type PairState =
	| { state: 'qr'; modules: number[][] }
	| { state: 'paired'; name: string }
	| { state: 'failed'; reason: string }

/** Written atomically so the dashboard never reads half a file. */
export function writePairState(path: string, s: PairState): void {
	const tmp = `${path}.tmp`
	writeFileSync(tmp, JSON.stringify({ ...s, ts: Date.now() }))
	renameSync(tmp, path)
}
