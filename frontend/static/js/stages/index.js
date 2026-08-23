const LOADERS = {
  created: () => import("./input.js?v=2026.08.23.100340566066"),
  clarification: () => import("./input.js?v=2026.08.23.100340566066"),
  narrative: () => import("./planning.js?v=2026.08.23.100340566066"),
  outline: () => import("./planning.js?v=2026.08.23.100340566066"),
  sample: () => import("./sample.js?v=2026.08.23.100340566066"),
  deck: () => import("./deck.js?v=2026.08.23.100340566066"),
  review: () => import("./review.js?v=2026.08.23.100340566066"),
  delivery: () => import("./delivery.js?v=2026.08.23.100340566066"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
