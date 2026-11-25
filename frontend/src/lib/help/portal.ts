// Help for supplier-portal users, published at /portal/help (decisions §508).
//
// Suppliers are a separate audience from the employee help centre's: they
// submit invoices, watch them get paid, change their bank details and take card
// payments, and they cannot open /help (a vendor user holds no employee
// session). So their guides use the same `Guide` shape, prose grammar and
// tests as the help centre's, marked `audience: 'supplier'`, and are kept OUT
// of `GUIDES`: the employee index, search and role lists never show them, and
// rendering them never loads the employee guide index (content.ts).
//
// The prose is English, as the help centre's is (rendered `lang="en"` under
// another UI locale, behind EnglishNotice); every control is named by
// `{ui:key}`, so it reads in the supplier's language and a rename fails
// content.test.ts. Every step here was checked against the portal routes
// (routes/portal/), the portal API (backend/app/api/portal.py) and
// backend/docs/supplier-portal.md, dynamic-discounting.md and supplier-chat.md
// — change them together.

import type { MessageKey } from '#lib/i18n/messages.ts';
import type { Guide } from './types.ts';

/**
 * The portal pages a supplier guide can open, with their portal-nav label.
 * A guide's `route` must be one of these.
 */
export const PORTAL_PAGES: Record<string, MessageKey> = {
	'/portal': 'portal.nav.overview',
	'/portal/invoices': 'portal.nav.invoices',
	'/portal/purchase-orders': 'portal.nav.purchaseOrders',
	'/portal/payments': 'portal.nav.payments',
	'/portal/discount-offers': 'portal.nav.discounts',
	'/portal/company': 'portal.nav.company',
	'/portal/notifications': 'portal.nav.notifications'
};

/**
 * The glossary terms the supplier help shows: a subset of the help centre's
 * glossary whose name and one-line definition (translated, `help.term.*`) read
 * true for a supplier. The employee entries' long text is written for the AP
 * team and links into /help, so the portal shows only the one-liners.
 */
export const PORTAL_GLOSSARY: string[] = [
	'supplier-portal',
	'invoice',
	'extraction',
	'e-invoice',
	'mfa',
	'bank-change-request',
	'payment-rail',
	'settlement',
	'virtual-card',
	'dynamic-discounting'
];

export const PORTAL_GUIDES: Guide[] = [
	{
		id: 'portal-getting-started',
		audience: 'supplier',
		kind: 'start',
		title: 'Getting started with the supplier portal',
		summary:
			'Set your password from the invitation email, sign in, turn on two-factor sign-in, and find your way around.',
		route: '/portal',
		sections: [
			{
				heading: 'Your invitation',
				blocks: [
					{
						type: 'p',
						text: 'Your customer’s accounts-payable team creates your portal account. You then get an email with a link to set your password. The link works once, and the AP team never sees or chooses your password.'
					},
					{
						type: 'steps',
						items: [
							'Open the link in the invitation email.',
							'Choose a password and type it again to confirm it.',
							'Tick the box to confirm you have read the platform’s Privacy Policy. It explains how the platform handles the bank details, tax ID and invoices you enter. You cannot set a password without ticking it.',
							'Select {ui:auth.resetPassword.setSubmit}, then sign in with your email address and the new password.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'If the link has expired or was already used, select {ui:auth.login.forgotPassword} on the sign-in page and we email you a new one. Your customer’s AP team can also send one.'
					}
				]
			},
			{
				heading: 'Signing in',
				blocks: [
					{
						type: 'p',
						text: 'Sign in with your email address and password. If you have turned on two-factor sign-in, you are then asked for the 6-digit code from your authenticator app. If you don’t have the app with you, select {ui:portal.login.mfa.useEmail} and we email a one-time code to your account address instead.'
					},
					{
						type: 'p',
						text: 'When the Privacy Policy changes, the portal asks you to confirm you have read the new version before it shows anything else. Read it, tick the box and select {ui:shell.privacyAck.continue}, or log out if you’d rather not.'
					}
				]
			},
			{
				heading: 'Turn on two-factor sign-in',
				blocks: [
					{
						type: 'p',
						text: '[[mfa|Two-factor sign-in]] asks for a code from an app on your phone as well as your password, so someone who learns your password still cannot get in. It is optional, and we recommend it: this account can change where your customer sends your payments.'
					},
					{
						type: 'steps',
						items: [
							'Open [[page:/portal/company|your company page]] and find {ui:portal.company.mfa.title}.',
							'Select {ui:portal.company.mfa.setUp}.',
							'Scan the QR code with an authenticator app (or type the key shown under it), then enter the 6-digit code the app shows.',
							'Select {ui:portal.company.mfa.confirm}.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'To set up a new authenticator once two-factor is on, the portal asks for your password or a current code first. That stops someone who borrows a signed-in browser from swapping your second factor for theirs. To turn it off, select {ui:portal.company.mfa.turnOff} and enter a current code.'
					}
				]
			},
			{
				heading: 'Finding your way around',
				blocks: [
					{
						type: 'list',
						items: [
							'{ui:portal.nav.overview} — what needs you ({ui:portal.home.kpi.actionRequired}), what is still with your customer, and what has been paid.',
							'{ui:portal.nav.invoices} — submit invoices and see where each one is.',
							'{ui:portal.nav.purchaseOrders} — your customer’s orders to you; you can turn one into an invoice.',
							'{ui:portal.nav.payments} — payments made to you, with remittance advice to download.',
							'{ui:portal.nav.discounts} — offers to pay you early in return for a discount ([[dynamic-discounting]]).',
							'{ui:portal.nav.company} — your contact details, bank details, tax ID and two-factor sign-in.',
							'{ui:portal.nav.notifications} — which emails you get about your invoices.'
						]
					}
				]
			}
		],
		terms: ['supplier-portal', 'mfa'],
		related: ['portal-submit-invoice', 'portal-track-payment', 'portal-chat']
	},
	{
		id: 'portal-submit-invoice',
		audience: 'supplier',
		kind: 'howto',
		title: 'Submit and resubmit an invoice',
		summary: 'Upload an invoice for your customer to review, and send a corrected one back when it is rejected.',
		route: '/portal/invoices',
		sections: [
			{
				heading: 'Submit an invoice',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/portal/invoices|your invoices]] and select {ui:portal.invoices.submit}.',
							'Choose the invoice file: a PDF or a photo.',
							'The invoice appears in your list as {ui:portal.invoices.phase.submitted}. Its details are read from the document automatically ([[extraction]]) and then reviewed by your customer’s AP team.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'One invoice per file. If your customer sent you a purchase order, you can also create the invoice from the order itself: see [[guide:portal-po-invoice|Create an invoice from a purchase order]].'
					}
				]
			},
			{
				heading: 'When an invoice is rejected',
				blocks: [
					{
						type: 'p',
						text: 'A rejected invoice shows {ui:portal.invoices.phase.rejected} with the reason under {ui:portal.invoices.rejectionReasonLabel}, and it is counted under {ui:portal.home.kpi.actionRequired} on your overview. It is the only kind of invoice you can change.'
					},
					{
						type: 'steps',
						items: [
							'Correct the invoice in your own system, as the reason asks.',
							'On the rejected invoice’s row, select {ui:portal.invoices.resubmit} and choose the corrected file.',
							'The corrected document replaces the old one on the same invoice, is read again, and goes back to the AP team for review.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'A resubmitted invoice always goes back to a person. It is never approved automatically, even where your customer approves some invoices without review, because someone already rejected this one.'
					}
				]
			}
		],
		terms: ['invoice', 'extraction', 'e-invoice'],
		related: ['portal-track-payment']
	},
	{
		id: 'portal-track-payment',
		audience: 'supplier',
		kind: 'howto',
		title: 'Track an invoice to payment',
		summary: 'Follow an invoice from submission to payment, download remittance advice, and choose which emails you get.',
		route: '/portal/payments',
		sections: [
			{
				heading: 'Where an invoice is',
				blocks: [
					{
						type: 'p',
						text: 'Each invoice on [[page:/portal/invoices|your invoices]] shows one stage, and the chips above the list filter by it:'
					},
					{
						type: 'list',
						items: [
							'{ui:portal.invoices.phase.submitted} and {ui:portal.invoices.phase.processing} — received, and its details are being read.',
							'{ui:portal.invoices.phase.underReview} — with your customer’s AP team. The row says what it is waiting on and for how many days.',
							'{ui:portal.invoices.phase.approved} — approved for payment.',
							'{ui:portal.invoices.phase.paymentScheduled} — included in a payment your customer has scheduled.',
							'{ui:portal.invoices.phase.paid} — paid.',
							'{ui:portal.invoices.phase.completed} — closed. Your customer can close an invoice without a payment here, so completed does not always mean paid.',
							'{ui:portal.invoices.phase.rejected} — sent back to you; see [[guide:portal-submit-invoice|Submit and resubmit an invoice]].'
						]
					}
				]
			},
			{
				heading: 'Your payments',
				blocks: [
					{
						type: 'p',
						text: '[[page:/portal/payments|Your payments]] lists each payment to you with its invoice number, the method it was sent by ([[payment-rail]]), the amount, its status and the bank or processor reference. A payment moves from {ui:portal.payments.phase.scheduled} through {ui:portal.payments.phase.processing} to {ui:portal.payments.phase.completed}; one can also end {ui:portal.payments.phase.failed} or {ui:portal.payments.phase.cancelled}.'
					},
					{
						type: 'p',
						text: 'Select {ui:portal.payments.downloadRemittance} on a payment for a PDF of what it paid, to match against your own records.'
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'Under {ui:portal.nav.notifications} you choose whether we email you when {ui:portal.notifications.onPaid} and when {ui:portal.notifications.onRejected}.'
					}
				]
			}
		],
		terms: ['payment-rail', 'settlement'],
		related: ['portal-card-payments', 'portal-bank-details']
	},
	{
		id: 'portal-bank-details',
		audience: 'supplier',
		kind: 'howto',
		title: 'Change your bank details',
		summary: 'Ask your customer to pay a new bank account. The change takes effect only once their AP team approves it.',
		route: '/portal/company',
		sections: [
			{
				heading: 'Request the change',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/portal/company|your company page]] and find {ui:portal.company.bank.title}. Your current details are not shown there, only whether some are on file.',
							'Enter the bank name and the account number.',
							'Enter the two-letter country of the bank in {ui:portal.company.bank.country}. For a UK bank (GB) the form asks for a {ui:portal.company.bank.sortCode}; otherwise it asks for a {ui:portal.company.bank.routing}.',
							'Select {ui:portal.company.bank.request}.'
						]
					}
				]
			},
			{
				heading: 'What happens next',
				blocks: [
					{
						type: 'p',
						text: 'The request waits for your customer’s AP team ([[bank-change-request]]). Until someone there approves it, your payments keep going to the account already on file. A banner on your company page, and {ui:portal.home.pendingChange.title} on your overview, show it is waiting.'
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'This is a fraud control. A common scam is an email that pretends to be from a supplier and asks for payments to go to a new account. Because no change applies until your customer checks it, a stolen password alone cannot redirect your money.'
					},
					{
						type: 'p',
						text: 'You can have one bank-detail request waiting at a time. A tax-ID change works the same way, from {ui:portal.company.tax.title} on the same page. Contact details save at once, and so does a W-9 or W-8 uploaded under {ui:portal.company.taxForm.title}.'
					}
				]
			}
		],
		terms: ['bank-change-request'],
		related: ['portal-tax-form', 'portal-track-payment', 'portal-getting-started']
	},
	{
		id: 'portal-card-payments',
		audience: 'supplier',
		kind: 'howto',
		title: 'Get paid by virtual card',
		summary: 'Your customer may pay an invoice with a single-use card. You get an email with a link that shows the card details once.',
		sections: [
			{
				heading: 'The card email',
				blocks: [
					{
						type: 'p',
						text: 'When your customer pays an invoice by [[virtual-card]], we email the address in your company details with the invoice number, the amount, and a link to the card. You do not need to sign in to open it: the link itself is the key.'
					},
					{
						type: 'steps',
						items: [
							'Open the link. The page shows the card number, CVV and expiry date, with {ui:portal.cards.copy} beside each.',
							'Charge the card for the invoice amount straight away, or store the details somewhere safe, before you close the page.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'The link opens once and expires 7 days after the card is issued. The page will not show the card again, even if it could not load the full card number the first time. If you lose the details, reply to the email and ask your customer to issue a new card.'
					}
				]
			},
			{
				heading: 'After you charge it',
				blocks: [
					{
						type: 'p',
						text: 'The card is limited to the amount in the email. To check where the invoice stands afterwards, see [[guide:portal-track-payment|Track an invoice to payment]].'
					}
				]
			}
		],
		terms: ['virtual-card'],
		related: ['portal-track-payment']
	},
	{
		id: 'portal-po-invoice',
		audience: 'supplier',
		kind: 'howto',
		title: 'Create an invoice from a purchase order',
		summary:
			'Turn one of your customer’s purchase orders into an invoice in one step, and know when to submit your own invoice instead.',
		route: '/portal/purchase-orders',
		sections: [
			{
				heading: 'Create the invoice',
				blocks: [
					{
						type: 'p',
						text: '[[page:/portal/purchase-orders|The purchase orders page]] lists the orders your customer has issued to you, with each one’s lines, total and status.'
					},
					{
						type: 'steps',
						items: [
							'Find the order and select {ui:portal.po.createInvoice} on its row.',
							'The invoice is made from the whole order: every line, at the order’s quantities and prices, for its full total and in its currency.',
							'You land on [[page:/portal/invoices|your invoices]], where the new invoice goes to your customer’s AP team for review like any other.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'Selecting it twice never makes two invoices. Once it is created, the order shows {ui:portal.po.refusal.alreadyInvoiced}.'
					}
				]
			},
			{
				heading: 'When you cannot create one',
				blocks: [
					{
						type: 'list',
						items: [
							'{ui:portal.po.refusal.alreadyInvoiced} — you have already billed this order, from here or with an invoice of your own. Submit an invoice for anything still owed instead.',
							'{ui:portal.po.refusal.notInvoiceable} — the order was closed or cancelled, so there is nothing left to bill on it.',
							'{ui:portal.po.totalUnverifiable} — your customer is re-checking the order’s total, so the button is unavailable. Submit your own invoice for it instead.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'Billing only part of an order, say for a partial delivery? Submit your own invoice for what you delivered, with the order number on it, rather than creating one from the order: that always bills the whole order.'
					}
				]
			}
		],
		terms: ['invoice'],
		related: ['portal-submit-invoice', 'portal-track-payment']
	},
	{
		id: 'portal-early-payment',
		audience: 'supplier',
		kind: 'howto',
		title: 'Accept an early-payment offer',
		summary:
			'Your customer may offer to pay you sooner in return for a small discount. Compare the tiers, then accept or decline.',
		route: '/portal/discount-offers',
		sections: [
			{
				heading: 'Reading an offer',
				blocks: [
					{
						type: 'p',
						text: 'Offers your customer makes you ([[dynamic-discounting]]) appear on [[page:/portal/discount-offers|the early-payment page]]. Each one covers either a single invoice or {ui:portal.discounts.scopeAll}.'
					},
					{
						type: 'list',
						items: [
							'{ui:portal.discounts.col.tiers} — each tier is a deadline and a discount: be paid within that many days of the start of the offer for that percentage off.',
							'{ui:portal.discounts.col.bestDiscount} and {ui:portal.discounts.col.youSave} — the best tier you can still get today, and how much it takes off.',
							'{ui:portal.discounts.col.window} — the dates the offer is open. After the last one it shows {ui:discounts.status.expired} and can no longer be accepted.'
						]
					}
				]
			},
			{
				heading: 'Accept or decline',
				blocks: [
					{
						type: 'steps',
						items: [
							'On an {ui:discounts.status.offered} offer, select {ui:portal.discounts.accept}.',
							'If it has more than one tier, pick one under {ui:portal.discounts.dialog.chooseTier}. The best tier still open is chosen for you, and the dialog shows what you would save.',
							'Select {ui:portal.discounts.dialog.accept}. The offer now shows {ui:discounts.status.accepted}.'
						]
					},
					{
						type: 'p',
						text: 'Accepting moves no money by itself. Your customer still schedules the payment; if it is paid by the tier’s deadline, the discount is taken off it and the offer shows {ui:discounts.status.captured}. If the deadline is missed, you are paid the full amount.'
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'To turn an offer down, select {ui:portal.discounts.decline}, then {ui:portal.discounts.declineConfirm}. You cannot undo a decline or an accept from the portal, and an invoice that is already paid cannot take a discount.'
					}
				]
			}
		],
		terms: ['dynamic-discounting'],
		related: ['portal-track-payment']
	},
	{
		id: 'portal-tax-form',
		audience: 'supplier',
		kind: 'howto',
		title: 'Upload your W-9 or W-8',
		summary: 'Give your customer the signed tax form they need for their US tax reporting, and replace it when it changes.',
		route: '/portal/company',
		sections: [
			{
				heading: 'Upload the form',
				blocks: [
					{
						type: 'p',
						text: 'US suppliers give their customer a signed W-9, and suppliers outside the US a W-8, so the customer can meet its 1099 and withholding obligations.'
					},
					{
						type: 'steps',
						items: [
							'Open [[page:/portal/company|your company page]] and find {ui:portal.company.taxForm.title}.',
							'Under {ui:portal.company.taxForm.formType}, choose {ui:portal.company.taxForm.optionW9} or {ui:portal.company.taxForm.optionW8}.',
							'Under {ui:portal.company.taxForm.signedForm}, choose the file: a PDF, PNG, JPEG or TIFF of up to 25 MB.',
							'Select {ui:portal.company.taxForm.upload}. The section then shows which form is on file and the date we received it.'
						]
					}
				]
			},
			{
				heading: 'Replacing it',
				blocks: [
					{
						type: 'p',
						text: 'Once a form is on file, select {ui:portal.company.taxForm.download} to check what your customer has, or upload a new one the same way and select {ui:portal.company.taxForm.replace}. The new form takes the old one’s place straight away.'
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'The form is a document, so it does not wait for approval. A change to the tax ID your customer holds for you does: request it under {ui:portal.company.tax.title}, as described in [[guide:portal-bank-details|Change your bank details]].'
					}
				]
			}
		],
		related: ['portal-bank-details']
	},
	{
		id: 'portal-chat',
		audience: 'supplier',
		kind: 'howto',
		title: 'Message your customer about an invoice',
		summary:
			'Each invoice has its own conversation with your customer’s AP team. Ask a question, answer one, or send a file.',
		route: '/portal/invoices',
		sections: [
			{
				heading: 'Open an invoice’s conversation',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/portal/invoices|your invoices]] and select the invoice number on its row. The conversation opens under the row; select the number again to close it.',
							'Type your message, or select {ui:supplierChat.attach} to add a file: a PDF, PNG, JPEG, TIFF or XML of up to 25 MB.',
							'Select {ui:supplierChat.send}.'
						]
					},
					{
						type: 'p',
						text: 'Your customer’s AP managers are told when you write. Messages cannot be edited or deleted once sent, and both sides see the same conversation.'
					}
				]
			},
			{
				heading: 'Replies',
				blocks: [
					{
						type: 'p',
						text: 'When someone on the AP team writes, we may email the address in your company details to say there is a new message on the invoice. The email does not contain the message itself: select its link to open that invoice’s conversation. If you are not signed in, the portal asks you to sign in first and then takes you there.'
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'The AP team can mark a conversation {ui:supplierChat.status.resolved} once a question is settled. You can still write in it. If your customer has turned conversations off, sending fails and says so.'
					}
				]
			}
		],
		related: ['portal-submit-invoice', 'portal-track-payment']
	}
];

const byId = new Map(PORTAL_GUIDES.map((g) => [g.id, g]));
export const portalGuideFor = (id: string): Guide | undefined => byId.get(id);
