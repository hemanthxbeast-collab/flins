import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { WAMessage } from '@whiskeysockets/baileys'
import { shouldIgnoreJid, toContactInfo, toInbound } from '../mapping.js'

const ME = '919000000000@s.whatsapp.net'
const RAHUL_PN = '919111111111@s.whatsapp.net'
const RAHUL_LID = '123456789012345@lid'
const GROUP = '120363000000000000@g.us'

function msg(partial: Partial<WAMessage>): WAMessage {
	return { messageTimestamp: 1_762_500_000, ...partial } as WAMessage
}

test('plain text from a phone-number contact', () => {
	const m = toInbound(
		msg({ key: { remoteJid: RAHUL_PN, id: 'M1', fromMe: false }, pushName: 'Rahul', message: { conversation: 'hey are you free?' } }),
		'notify',
		ME
	)
	assert.ok(m)
	assert.equal(m.type, 'text')
	assert.equal(m.text, 'hey are you free?')
	assert.equal(m.sender_jid, RAHUL_PN)
	assert.equal(m.is_group, false)
	assert.equal(m.ts_ms, 1_762_500_000_000)
	assert.equal(m.push_name, 'Rahul')
})

test('LID sender keeps the phone-number alternative', () => {
	const m = toInbound(
		msg({ key: { remoteJid: RAHUL_LID, remoteJidAlt: RAHUL_PN, id: 'M2', fromMe: false }, message: { conversation: 'hi' } }),
		'notify',
		ME
	)
	assert.equal(m?.sender_jid, RAHUL_LID)
	assert.equal(m?.sender_jid_alt, RAHUL_PN)
	assert.equal(m?.chat_jid_alt, RAHUL_PN)
})

test('quoted group text with mentions', () => {
	const m = toInbound(
		msg({
			key: { remoteJid: GROUP, id: 'M3', fromMe: false, participant: RAHUL_LID, participantAlt: RAHUL_PN },
			message: {
				extendedTextMessage: {
					text: '@Hemanth see this',
					contextInfo: { stanzaId: 'Q1', mentionedJid: [ME] }
				}
			}
		}),
		'notify',
		ME
	)
	assert.equal(m?.is_group, true)
	assert.equal(m?.sender_jid, RAHUL_LID)
	assert.equal(m?.sender_jid_alt, RAHUL_PN)
	assert.equal(m?.quoted_id, 'Q1')
	assert.deepEqual(m?.mentions, [ME])
})

test('image with caption, reaction, and own message', () => {
	const img = toInbound(
		msg({ key: { remoteJid: RAHUL_PN, id: 'M4' }, message: { imageMessage: { caption: 'look' } } }),
		'notify',
		ME
	)
	assert.equal(img?.type, 'image')
	assert.equal(img?.text, 'look')

	const react = toInbound(
		msg({ key: { remoteJid: RAHUL_PN, id: 'M5' }, message: { reactionMessage: { text: '👍', key: { id: 'M4' } } } }),
		'notify',
		ME
	)
	assert.equal(react?.type, 'reaction')
	assert.equal(react?.text, '👍')
	assert.equal(react?.quoted_id, 'M4')

	const own = toInbound(
		msg({ key: { remoteJid: RAHUL_PN, id: 'M6', fromMe: true }, message: { conversation: 'sent from phone' } }),
		'append',
		ME
	)
	assert.equal(own?.from_me, true)
	assert.equal(own?.sender_jid, ME)
	assert.equal(own?.upsert_type, 'append')
})

test('skips housekeeping, empty and ignored chats', () => {
	const proto = toInbound(
		msg({ key: { remoteJid: RAHUL_PN, id: 'P1' }, message: { protocolMessage: { type: 0 } } }),
		'notify',
		ME
	)
	assert.equal(proto, null)
	assert.equal(toInbound(msg({ key: { remoteJid: RAHUL_PN, id: 'E1' } }), 'notify', ME), null)
	assert.equal(
		toInbound(msg({ key: { remoteJid: 'status@broadcast', id: 'S1' }, message: { conversation: 'x' } }), 'notify', ME),
		null
	)
	assert.ok(shouldIgnoreJid('status@broadcast'))
	assert.ok(shouldIgnoreJid('120363000000000000@newsletter'))
	assert.ok(!shouldIgnoreJid(RAHUL_PN))
	assert.ok(!shouldIgnoreJid(GROUP))
})

test('contact info picks phone number and LID from any field', () => {
	assert.deepEqual(toContactInfo({ id: RAHUL_LID, phoneNumber: RAHUL_PN, name: 'Rahul Work' }), {
		pn: RAHUL_PN,
		lid: RAHUL_LID,
		name: 'Rahul Work',
		notify: null
	})
	assert.equal(toContactInfo({ id: GROUP }), null)
})
