import { ensureSlices } from '#lib/i18n/store.svelte.ts';

// The supplier help pages render glossary term names and one-liners and the
// help chrome (`help.*`), which live in the help slice (decisions §261). The
// parent /portal layout already loads the portal slice. Deliberately NOT the
// settings slice the employee /help loads: supplier guides name only portal,
// auth and shell controls, and their pages never import the employee guide
// index (decisions §509).
export const load = () => ensureSlices('help');
