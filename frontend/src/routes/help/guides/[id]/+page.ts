import type { PageLoad } from './$types';
import { guideFor } from '#lib/help/content.ts';
import { loadDiagrams } from '#lib/components/help/diagrams/Diagram.svelte';

// The guide's drawings are lazy chunks (`Diagram.svelte`); SvelteKit awaits
// this before rendering, so a guide's diagram is drawn on its first frame, as
// it was when every drawing was bundled into the route.
export const load: PageLoad = async ({ params }) => {
	const guide = guideFor(params.id);
	if (!guide) return;
	await loadDiagrams(
		guide.sections.flatMap((s) => s.blocks.flatMap((b) => (b.type === 'diagram' ? [b.id] : [])))
	);
};
