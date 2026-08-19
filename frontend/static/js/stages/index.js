const LOADERS = {
  created: () => import("./input.js?v=2026.08.19.081751755538"),
  clarification: () => import("./input.js?v=2026.08.19.081751755538"),
  narrative: () => import("./planning.js?v=2026.08.19.081751755538"),
  outline: () => import("./planning.js?v=2026.08.19.081751755538"),
  sample: () => import("./sample.js?v=2026.08.19.081751755538"),
  deck: () => import("./deck.js?v=2026.08.19.081751755538"),
  review: () => import("./review.js?v=2026.08.19.081751755538"),
  delivery: () => import("./delivery.js?v=2026.08.19.081751755538"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
