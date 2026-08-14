const LOADERS = {
  created: () => import("./input.js?v=2026.08.14.2"),
  clarification: () => import("./input.js?v=2026.08.14.2"),
  narrative: () => import("./planning.js?v=2026.08.14.2"),
  outline: () => import("./planning.js?v=2026.08.14.2"),
  sample: () => import("./sample.js?v=2026.08.14.2"),
  deck: () => import("./deck.js?v=2026.08.14.2"),
  review: () => import("./review.js?v=2026.08.14.2"),
  delivery: () => import("./delivery.js?v=2026.08.14.2"),
};

export async function renderStage(stage, context) {
  const load = LOADERS[stage];
  if (!load) throw new Error(`Unknown stage: ${stage}`);
  const module = await load();
  return module.render(context);
}
