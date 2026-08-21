const LOADERS = {
  created: () => import("./input.js?v=2026.08.21.075744095363"),
  clarification: () => import("./input.js?v=2026.08.21.075744095363"),
  narrative: () => import("./planning.js?v=2026.08.21.075744095363"),
  outline: () => import("./planning.js?v=2026.08.21.075744095363"),
  sample: () => import("./sample.js?v=2026.08.21.075744095363"),
  deck: () => import("./deck.js?v=2026.08.21.075744095363"),
  review: () => import("./review.js?v=2026.08.21.075744095363"),
  delivery: () => import("./delivery.js?v=2026.08.21.075744095363"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
