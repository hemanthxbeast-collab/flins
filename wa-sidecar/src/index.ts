// flins-wa: WhatsApp linked-device sidecar for FLINS. READ-ONLY in M1: it has no way to send.
//
//   node dist/index.js --pair --auth-dir <dir>                 show a QR and pair, then exit
//   node dist/index.js --pair --auth-dir <dir> --qr-file <f>   same, QR also as JSON for the dashboard
//   node dist/index.js --port 8766 --auth-dir <dir>            run (needs FLINS_WA_SECRET)

import { existsSync, readFileSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import type { Boom } from '@hapi/boom'
import makeWASocket, {
	Browsers,
	type Contact,
	DisconnectReason,
	fetchLatestWaWebVersion,
	isJidGroup,
	isLidUser,
	useMultiFileAuthState,
	type WASocket
} from '@whiskeysockets/baileys'
import pino from 'pino'
import qrcode from 'qrcode-terminal'
import { type ContactInfo, shouldIgnoreJid, toContactInfo, toInbound } from './mapping.js'
import { MIN_SECRET_LENGTH } from './protocol.js'
import { qrMatrix, writePairState } from './qr.js'
import { CoreServer } from './server.js'

const SIDECAR_VERSION = '0.1.0'
const BAILEYS_VERSION = '7.0.0-rc14'
const CONTACT_CHUNK = 200

interface Args {
	pair: boolean
	port: number
	authDir: string
	qrFile: string | null
}

function parseArgs(argv: string[]): Args {
	const args: Args = { pair: false, port: 8766, authDir: 'data/wa-auth', qrFile: null }
	for (let i = 0; i < argv.length; i++) {
		const a = argv[i]
		if (a === '--pair') args.pair = true
		else if (a === '--port') args.port = Number(argv[++i])
		else if (a === '--auth-dir') args.authDir = String(argv[++i])
		else if (a === '--qr-file') args.qrFile = String(argv[++i])
		else throw new Error(`unknown argument ${a}`)
	}
	if (!Number.isInteger(args.port) || args.port < 1024 || args.port > 65535) {
		throw new Error('--port must be 1024-65535')
	}
	return args
}

// Our own log lines go to stderr. They never include message text.
const log = (msg: string): void => {
	process.stderr.write(`[flins-wa] ${msg}\n`)
}

function isPaired(authDir: string): boolean {
	const credsPath = join(authDir, 'creds.json')
	if (!existsSync(credsPath)) return false
	try {
		const creds = JSON.parse(readFileSync(credsPath, 'utf8')) as { me?: unknown }
		return Boolean(creds.me)
	} catch {
		return false
	}
}

function moveAuthAside(authDir: string): string | null {
	if (!existsSync(authDir)) return null
	const target = `${authDir}.loggedout-${Date.now()}`
	renameSync(authDir, target)
	return target
}

async function waVersion(): Promise<[number, number, number] | undefined> {
	try {
		const { version, error } = await fetchLatestWaWebVersion()
		return error ? undefined : version
	} catch {
		return undefined
	}
}

function statusCode(err: unknown): number | undefined {
	return (err as Boom | undefined)?.output?.statusCode
}

async function openSocket(authDir: string): Promise<{ sock: WASocket; saveCreds: () => Promise<void> }> {
	const { state, saveCreds } = await useMultiFileAuthState(authDir)
	const version = await waVersion()
	const sock = makeWASocket({
		auth: state,
		...(version ? { version } : {}),
		browser: Browsers.windows('Desktop'),
		logger: pino({ level: 'warn' }, pino.destination(2)),
		markOnlineOnConnect: false, // keep the phone getting notifications
		syncFullHistory: false,
		shouldIgnoreJid: jid => shouldIgnoreJid(jid),
		getMessage: async () => undefined
	})
	sock.ev.on('creds.update', saveCreds)
	return { sock, saveCreds }
}

// ---------------------------------------------------------------- pairing mode

async function runPair(authDir: string, qrFile: string | null): Promise<number> {
	const state = (s: Parameters<typeof writePairState>[1]): void => {
		if (qrFile) writePairState(qrFile, s)
	}
	if (isPaired(authDir)) {
		log(`already paired (auth in ${authDir}). Use "flins wa pair --force" to re-pair.`)
		state({ state: 'paired', name: '' })
		return 0
	}
	return new Promise(resolve => {
		const attempt = async (): Promise<void> => {
			const { sock } = await openSocket(authDir)
			sock.ev.on('connection.update', update => {
				if (update.qr) {
					log('Scan this QR with WhatsApp > Linked devices > Link a device:')
					qrcode.generate(update.qr, { small: true })
					state({ state: 'qr', modules: qrMatrix(update.qr) })
				}
				if (update.connection === 'open') {
					log(`Paired as ${sock.user?.name ?? ''} (${sock.user?.id ?? '?'})`)
					state({ state: 'paired', name: sock.user?.name ?? '' })
					// Give Baileys a moment to finish writing credentials.
					setTimeout(() => {
						sock.end(undefined)
						resolve(0)
					}, 3000)
				}
				if (update.connection === 'close') {
					const code = statusCode(update.lastDisconnect?.error)
					if (code === DisconnectReason.restartRequired) {
						void attempt() // normal right after scanning
					} else if (code === DisconnectReason.timedOut) {
						log('QR expired. Run the pair command again.')
						state({ state: 'failed', reason: 'The QR expired before it was scanned.' })
						resolve(1)
					} else if (!isPaired(authDir)) {
						log(`pairing failed (code ${code ?? '?'})`)
						state({ state: 'failed', reason: `Pairing failed (code ${code ?? '?'}).` })
						resolve(1)
					}
				}
			})
		}
		void attempt()
	})
}

// ---------------------------------------------------------------- run mode

async function runServe(args: Args): Promise<void> {
	const secret = process.env.FLINS_WA_SECRET ?? ''
	if (secret.length < MIN_SECRET_LENGTH) {
		log(`FLINS_WA_SECRET must be set and at least ${MIN_SECRET_LENGTH} characters`)
		process.exit(2)
	}

	let state: Record<string, unknown> = { state: 'connecting' }
	let sock: WASocket | null = null
	let stopped = false
	let backoffMs = 1000

	const shutdown = async (): Promise<void> => {
		if (stopped) return
		stopped = true
		log('shutting down')
		sock?.end(undefined)
		await server.close()
		process.exit(0)
	}

	const server = new CoreServer({
		port: args.port,
		secret,
		log,
		onShutdown: () => void shutdown(),
		onCommand: (kind, payload) => {
			if (kind === 'send_text') void handleSend(payload)
			else log(`ignored unknown command kind=${kind}`)
		},
		helloInfo: () => ({ sidecar_version: SIDECAR_VERSION, baileys_version: BAILEYS_VERSION, ...state })
	})

	// ---- sending (M4). Core's Outbox decides *whether* to send; this only refuses unsafe targets,
	// duplicates, and sending while disconnected. Plain text only: no media, no links preview.
	const sentIds = new Map<string, number>()
	const handleSend = async (p: Record<string, unknown>): Promise<void> => {
		const id = typeof p.client_msg_id === 'string' ? p.client_msg_id : ''
		const jid = typeof p.chat_jid === 'string' ? p.chat_jid : ''
		const text = typeof p.text === 'string' ? p.text : ''
		const reply = (ok: boolean, extra: Record<string, unknown> = {}): void =>
			server.emit('send.result', { client_msg_id: id, ok, ...extra })
		if (!id || !jid || !text.trim()) return reply(false, { error: 'bad request' })
		if (sentIds.has(id)) return reply(false, { error: 'duplicate client_msg_id', duplicate: true })
		if (shouldIgnoreJid(jid) || isJidGroup(jid)) return reply(false, { error: 'refusing to send to this chat' })
		if (text.length > 2000) return reply(false, { error: 'text too long' })
		const s = sock
		if (!s || state.state !== 'open') return reply(false, { error: 'WhatsApp is not connected' })
		sentIds.set(id, Date.now())
		for (const [k, t] of sentIds) if (Date.now() - t > 24 * 3600_000) sentIds.delete(k)
		try {
			await s.sendPresenceUpdate('composing', jid)
			await new Promise(r => setTimeout(r, Math.min(4000, 400 + text.length * 40)))
			const msg = await s.sendMessage(jid, { text }, { linkPreview: null } as never)
			await s.sendPresenceUpdate('paused', jid).catch(() => {})
			log(`sent message ${msg?.key?.id ?? '?'}`)
			reply(true, { wa_msg_id: msg?.key?.id ?? null })
		} catch (err) {
			reply(false, { error: err instanceof Error ? err.message.slice(0, 200) : 'send failed' })
		}
	}
	await server.start()
	log(`listening on 127.0.0.1:${server.port}`)

	const setState = (s: Record<string, unknown>): void => {
		state = s
		server.emit('connection', s)
	}

	const emitContacts = (contacts: ContactInfo[]): void => {
		for (let i = 0; i < contacts.length; i += CONTACT_CHUNK) {
			server.emit('contacts.upsert', { contacts: contacts.slice(i, i + CONTACT_CHUNK) })
		}
	}
	const fromBaileys = (list: Partial<Contact>[]): ContactInfo[] =>
		list.map(toContactInfo).filter((c): c is ContactInfo => c !== null)

	process.on('SIGINT', () => void shutdown())
	process.on('SIGTERM', () => void shutdown())

	if (!isPaired(args.authDir)) {
		log('not paired yet. Run "flins wa pair" first.')
		setState({ state: 'not_paired' })
		return // keep the server up so core can see the state
	}

	const connect = async (): Promise<void> => {
		if (stopped) return
		setState({ state: 'connecting' })
		const opened = await openSocket(args.authDir)
		sock = opened.sock
		const s = sock

		s.ev.on('connection.update', update => {
			if (update.qr) {
				// We have credentials but WhatsApp wants a QR: the session is no longer valid.
				log('session invalid (QR requested). Re-pair needed.')
				s.end(undefined)
				const moved = moveAuthAside(args.authDir)
				setState({ state: 'logged_out', reason_code: null, moved_auth_to: moved })
				stopped = true
				return
			}
			if (update.connection === 'open') {
				backoffMs = 1000
				const me = s.user
				log('connected to WhatsApp')
				setState({
					state: 'open',
					me: { id: me?.id ?? null, lid: me?.lid ?? null, name: me?.name ?? null }
				})
			}
			if (update.connection === 'close') {
				const code = statusCode(update.lastDisconnect?.error)
				if (stopped) return
				if (
					code === DisconnectReason.loggedOut ||
					code === DisconnectReason.forbidden ||
					code === DisconnectReason.multideviceMismatch
				) {
					const moved = moveAuthAside(args.authDir)
					log(`logged out by WhatsApp (code ${code}). Re-pair needed.`)
					setState({ state: 'logged_out', reason_code: code, moved_auth_to: moved })
					stopped = true
				} else if (code === DisconnectReason.connectionReplaced) {
					log('another WhatsApp Web session replaced this one. Stopping.')
					setState({ state: 'conflict', reason_code: code })
					stopped = true
				} else if (code === DisconnectReason.restartRequired) {
					void connect()
				} else {
					log(`connection closed (code ${code ?? '?'}); retrying in ${backoffMs / 1000}s`)
					setState({ state: 'close', reason_code: code ?? null, retry_in_ms: backoffMs })
					setTimeout(() => void connect(), backoffMs)
					backoffMs = Math.min(backoffMs * 2, 60_000)
				}
			}
		})

		s.ev.on('messages.upsert', async ({ messages, type }) => {
			const me = s.user?.id ?? null
			for (const m of messages) {
				const inbound = toInbound(m, type, me)
				if (!inbound) continue
				// Fill in the phone-number form of a LID sender when Baileys knows it.
				if (!inbound.sender_jid_alt && isLidUser(inbound.sender_jid)) {
					try {
						inbound.sender_jid_alt =
							(await s.signalRepository.lidMapping.getPNForLID(inbound.sender_jid)) ?? null
					} catch {
						// mapping unknown: core keeps the LID
					}
				}
				if (!inbound.is_group && !inbound.chat_jid_alt && inbound.sender_jid_alt && !inbound.from_me) {
					inbound.chat_jid_alt = inbound.sender_jid_alt
				}
				server.emit('message.received', inbound)
			}
		})

		s.ev.on('contacts.upsert', list => emitContacts(fromBaileys(list)))
		s.ev.on('contacts.update', list => emitContacts(fromBaileys(list)))
		s.ev.on('messaging-history.set', ({ contacts, lidPnMappings }) => {
			emitContacts(fromBaileys(contacts))
			if (lidPnMappings?.length) {
				emitContacts(lidPnMappings.map(m => ({ pn: m.pn, lid: m.lid, name: null, notify: null })))
			}
		})
		s.ev.on('lid-mapping.update', m => emitContacts([{ pn: m.pn, lid: m.lid, name: null, notify: null }]))
	}

	await connect()
}

// ---------------------------------------------------------------- main

async function main(): Promise<void> {
	const args = parseArgs(process.argv.slice(2))
	if (args.pair) {
		process.exit(await runPair(args.authDir, args.qrFile))
	}
	await runServe(args)
}

main().catch(err => {
	log(`fatal: ${err instanceof Error ? err.message : String(err)}`)
	process.exit(1)
})
