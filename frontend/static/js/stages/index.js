const LOADERS = {
  created: () => import("./input.js?v=2026.08.22.152316565533"),
  clarification: () => import("./input.js?v=2026.08.22.152316565533"),
  narrative: () => import("./planning.js?v=2026.08.22.152316565533"),
  outline: () => import("./planning.js?v=2026.08.22.152316565533"),
  sample: () => import("./sample.js?v=2026.08.22.152316565533"),
  deck: () => import("./deck.js?v=2026.08.22.152316565533"),
  review: () => import("./review.js?v=2026.08.22.152316565533"),
  delivery: () => import("./delivery.js?v=2026.08.22.152316565533"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
