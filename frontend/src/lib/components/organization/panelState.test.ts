import { describe, expect, it, vi } from 'vitest';

// The real store reaches `$app/env`; the Ollama label lives in the settings
// slice, so `m` reads that slice's English copy.
vi.mock('#lib/i18n/store.svelte.ts', async () => {
	const { enSettings } = await import('#lib/i18n/locales/settings/en.ts');
	return {
		m: (key: string) => (enSettings as Record<string, string>)[key] ?? key
	};
});

const { CARD_REGIONS, EXTRACTION_PROVIDERS, cardAutoProvider, extractionProviderLabel } =
	await import('./panelState');

describe('cardAutoProvider', () => {
	// Every listed region resolves to the provider the table names for it, so
	// a row added without a matching lookup cannot silently fall to the default.
	it.each(CARD_REGIONS.map((r) => [r.value, r.default_provider] as const))(
		'region %s → %s',
		(region, provider) => {
			expect(cardAutoProvider(region)).toBe(provider);
		}
	);

	it('Lithic covers the US, UK and EU regions; Nium the rest', () => {
		for (const r of CARD_REGIONS) {
			const lithic = r.value === 'US' || r.value === 'UK' || r.eu;
			expect(cardAutoProvider(r.value)).toBe(lithic ? 'lithic' : 'nium');
		}
	});

	it('falls back to Nium for a region it does not list', () => {
		expect(cardAutoProvider('XX')).toBe('nium');
		expect(cardAutoProvider('')).toBe('nium');
		// The stored value is `UK`, not ISO's `GB`.
		expect(cardAutoProvider('GB')).toBe('nium');
	});
});

describe('extractionProviderLabel', () => {
	it('names each listed provider by its product name', () => {
		for (const p of EXTRACTION_PROVIDERS.filter((p) => p.value !== 'ollama')) {
			expect(extractionProviderLabel(p.value)).toBe(p.name);
		}
	});

	it('labels Ollama through its keyed "(local)" copy', () => {
		expect(extractionProviderLabel('ollama')).toBe('Ollama (local)');
	});

	it('shows an unknown provider by its raw value rather than nothing', () => {
		expect(extractionProviderLabel('acme_ocr')).toBe('acme_ocr');
	});
});
