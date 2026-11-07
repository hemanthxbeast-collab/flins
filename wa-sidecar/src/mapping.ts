// Pure functions that turn Baileys objects into protocol payloads. No sockets here, so they're easy to test.

import {
	type Contact,
	getContentType,
	isJidBroadcast,
	isJidGroup,
	isJidMetaAI,
	isJidNewsletter,
	isJidStatusBroadcast,
	isLidUser,
	isPnUser,
	normalizeMessageContent,
	type proto,
	type WAMessage
} from '@whiskeysockets/baileys'

export type MsgType =
	| 'text'
	| 'image'
	| 'video'
	| 'audio'
	| 'doc'
	| 'sticker'
	| 'reaction'
	| 'location'
	| 'contact'
	| 'poll'
	| 'other'

export interface InboundMessage {
	[key: string]: unknown
	wa_msg_id: string
	chat_jid: string
	chat_jid_alt: string | null
	sender_jid: string
	sender_jid_alt: string | null
	from_me: boolean
	is_group: boolean
	push_name: string | null
	ts_ms: number
	type: MsgType
	text: string | null
	quoted_id: string | null
	mentions: string[]
	upsert_type: 'notify' | 'append'
}

export interface ContactInfo {
	[key: string]: unknown
	pn: string | null // phone-number JID, e.g. 9198...@s.whatsapp.net
	lid: string | null // LID JID, e.g. 1234...@lid
	name: string | null // name saved in the user's address book
	notify: string | null // name the contact set for themselves
}

const TYPE_MAP: Partial<Record<keyof proto.IMessage, MsgType>> = {
	conversation: 'text',
	extendedTextMessage: 'text',
	imageMessage: 'image',
	videoMessage: 'video',
	ptvMessage: 'video',
	audioMessage: 'audio',
	documentMessage: 'doc',
	documentWithCaptionMessage: 'doc',
	stickerMessage: 'sticker',
	reactionMessage: 'reaction',
	locationMessage: 'location',
	liveLocationMessage: 'location',
	contactMessage: 'contact',
	contactsArrayMessage: 'contact',
	pollCreationMessage: 'poll',
	pollCreationMessageV2: 'poll',
	pollCreationMessageV3: 'poll'
}

// Housekeeping messages that are not something a person "said".
const SKIP_TYPES = new Set<string>([
	'protocolMessage',
	'senderKeyDistributionMessage',
	'messageContextInfo',
	'pollUpdateMessage',
	'keepInChatMessage',
	'pinInChatMessage',
	'encReactionMessage',
	'callLogMesssage'
])

/** Chats FLINS never looks at: status updates, broadcast lists, channels, Meta AI. */
export function shouldIgnoreJid(jid: string | null | undefined): boolean {
	if (!jid) return true
	return Boolean(
		isJidStatusBroadcast(jid) || isJidBroadcast(jid) || isJidNewsletter(jid) || isJidMetaAI(jid)
	)
}

function toMs(ts: WAMessage['messageTimestamp'], fallback: number): number {
	if (typeof ts === 'number') return ts * 1000
	if (ts && typeof (ts as { toNumber?: () => number }).toNumber === 'function') {
		return (ts as { toNumber: () => number }).toNumber() * 1000
	}
	return fallback
}

/**
 * Convert one Baileys message. Returns null for anything FLINS should not see.
 * `me` is the user's own JID, used as the sender of messages they sent from their phone.
 */
export function toInbound(
	msg: WAMessage,
	upsertType: 'notify' | 'append',
	me: string | null,
	now: number = Date.now()
): InboundMessage | null {
	const key = msg.key
	const chat = key?.remoteJid
	if (!key?.id || !chat || shouldIgnoreJid(chat)) return null

	const content = normalizeMessageContent(msg.message)
	if (!content) return null
	const ctype = getContentType(content)
	if (!ctype || SKIP_TYPES.has(ctype)) return null

	const isGroup = Boolean(isJidGroup(chat))
	const fromMe = Boolean(key.fromMe)
	let sender: string
	let senderAlt: string | null
	if (fromMe) {
		sender = me ?? 'me'
		senderAlt = null
	} else if (isGroup) {
		sender = key.participant ?? 'unknown'
		senderAlt = key.participantAlt ?? null
	} else {
		sender = chat
		senderAlt = key.remoteJidAlt ?? null
	}

	let text: string | null = null
	let quoted: string | null = null
	let mentions: string[] = []

	const inner = content[ctype] as
		| { caption?: string | null; text?: string | null; contextInfo?: proto.IContextInfo | null }
		| string
		| null
		| undefined

	if (ctype === 'conversation') {
		text = content.conversation ?? null
	} else if (ctype === 'reactionMessage') {
		text = content.reactionMessage?.text ?? null
		quoted = content.reactionMessage?.key?.id ?? null
	} else if (ctype === 'documentWithCaptionMessage') {
		const doc = content.documentWithCaptionMessage?.message?.documentMessage
		text = doc?.caption ?? null
	} else if (inner && typeof inner === 'object') {
		text = inner.text ?? inner.caption ?? null
		const ctx = inner.contextInfo
		if (ctx) {
			quoted = ctx.stanzaId ?? null
			mentions = (ctx.mentionedJid ?? []).filter((j): j is string => typeof j === 'string')
		}
	}

	return {
		wa_msg_id: key.id,
		chat_jid: chat,
		chat_jid_alt: key.remoteJidAlt ?? null,
		sender_jid: sender,
		sender_jid_alt: senderAlt,
		from_me: fromMe,
		is_group: isGroup,
		push_name: msg.pushName ?? null,
		ts_ms: toMs(msg.messageTimestamp, now),
		type: TYPE_MAP[ctype] ?? 'other',
		text,
		quoted_id: quoted,
		mentions,
		upsert_type: upsertType
	}
}

/** Convert a Baileys contact (full or partial). Returns null if it has no usable id. */
export function toContactInfo(c: Partial<Contact>): ContactInfo | null {
	const ids = [c.id, c.lid, c.phoneNumber].filter((x): x is string => typeof x === 'string')
	const pn = ids.find(j => isPnUser(j)) ?? null
	const lid = ids.find(j => isLidUser(j)) ?? null
	if (!pn && !lid) return null
	return { pn, lid, name: c.name ?? null, notify: c.notify ?? null }
}
